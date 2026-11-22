"""Read-only regression/data-integrity checks and a machine-readable summary."""
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from capstone_rag import config,qms,index

out=Path('docs/phase4_2')
suite=ET.parse(out/'final_regression_pytest.xml').getroot().find('testsuite')
checks={'python_regression':dict(suite.attrib),'qms_fingerprint':index.corpus_fingerprint(qms.load_chunks())}
for name,path in [('operations',Path(config.DB_PATH)),('configuration',Path(config.DATA_DIR)/'ai_configuration.db')]:
    with sqlite3.connect('file:'+path.resolve().as_posix()+'?mode=ro',uri=True) as db:
        checks[name+'_integrity']=db.execute('pragma integrity_check').fetchone()[0]
        if name=='configuration':checks['preferences']=db.execute('select key,value from preferences').fetchall()
assert suite.attrib['failures']=='0' and suite.attrib['errors']=='0'
assert checks['operations_integrity']==checks['configuration_integrity']=='ok'
assert checks['qms_fingerprint']=='a3b68d86451db05e'
(out/'final_checks.json').write_text(json.dumps(checks,indent=2))
print(json.dumps(checks,indent=2))
