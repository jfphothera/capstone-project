"""Exhaustive reply-policy rule audit; never a full-system quality benchmark."""
import json
from pathlib import Path
import sys
from collections import Counter
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from capstone_rag.interpretation import interpret,validate_finding
source=json.loads((root/'docs/evidence/multi_agent/reference/ticket_audit.json').read_text())
rows=[]
for ticket in source:
    # Input contains body/observed transport only, no labels or expected facts.
    result=interpret(ticket['body'],'unknown',ticket['pool_id'])
    expected=ticket['outbound_policy']!='candidate_reply'
    actual=result['outbound_message']['decision']!='draft_for_review'
    rows.append({'input_id':ticket['pool_id'],'expected_no_auto_reply':expected,'actual_no_auto_reply':actual,
        'evidence_spans_valid':validate_finding(result,ticket['body']),'finding':result})
report={'scope':'All 1000 original bodies; reply-policy rule audit only; not independent human gold or an end-to-end model evaluation',
    'baseline_policy':'single-path unconditional drafting from identical source',
    'denominator':len(rows),'baseline_correct':sum(not r['expected_no_auto_reply'] for r in rows),
    'coordinated_correct':sum(r['expected_no_auto_reply']==r['actual_no_auto_reply'] for r in rows),
    'invalid_evidence_spans':sum(not r['evidence_spans_valid'] for r in rows),
    'message_types':dict(Counter(r['finding']['message_type'] for r in rows)),
    'mismatches':[r['input_id'] for r in rows if r['expected_no_auto_reply']!=r['actual_no_auto_reply']],'rows':rows}
path=root/'docs/evidence/multi_agent/ticket-interpretation-1000.json';path.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='rows'},indent=2))
