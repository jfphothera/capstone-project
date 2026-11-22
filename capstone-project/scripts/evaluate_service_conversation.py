"""Replay reviewed user examples in a disposable DB with isolated agent files."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--output', required=True)
parser.add_argument('--real', action='store_true')
args = parser.parse_args()
root = Path(args.root)
sys.path.insert(0, str(root / 'src'))
from capstone_rag import agent, cases, chat, config, memory, store

with tempfile.TemporaryDirectory(prefix='capstone-service-eval-') as temporary:
    td = Path(temporary)
    source = sqlite3.connect(f"file:{(root / 'data/operations.db').as_posix()}?mode=ro", uri=True)
    copy = sqlite3.connect(td / 'evaluation.db')
    source.backup(copy)
    source.close()
    copy.close()
    st = store.Store(str(td / 'evaluation.db'))
    ag = agent.Agent(prefer_llm=args.real,
                     escalations=cases.EscalationQueue(str(td / 'escalations.jsonl')),
                     approvals=cases.ApprovalQueue(str(td / 'approvals.jsonl')),
                     reviewed_memory=memory.ReviewedMemory(str(td / 'memory.jsonl')),
                     ticket_log_path=str(td / 'tickets.jsonl'))
    bot = chat.ChatBot(st, ag)
    journeys = [
        ('name-only', ['hi my p100 isnt working, the lamp wont turn on', 'john doe']),
        ('reviewed-correction', ['hi my device isnt working', 'mei murphy and my date of birth is 1954-04-18', 'its wei murphy', '52323377']),
        ('ambiguous-dob', ['wei murphy dob 04/05/1954']),
        ('authorized-lamp', ['my name is Wei Murphy. Serial 72377DL1. The unit powers on but the lamp stays dark.']),
    ]
    if args.real:
        journeys = [('real-conversational', ['my name is wei murphy']),
                    ('real-clarification', ['my name is Wei Murphy. Serial 52323377. My device isnt working.'])]
    result = {'mode': 'local_qwen3_requested' if args.real else 'deterministic',
              'root': str(root), 'model': config.OLLAMA_MODEL, 'journeys': []}
    for label, turns in journeys:
        session = bot.new_session()
        start = time.perf_counter()
        for turn in turns:
            bot.respond(session, turn)
        evidence = st.query('SELECT * FROM activity_events WHERE conversation_id=? ORDER BY event_id', (session.conversation_id,)) if st.one("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'") else []
        for event in evidence:
            event['evidence'] = json.loads(event['evidence'], parse_constant=lambda _: None)
        result['journeys'].append({'name': label, 'conversation_id': session.conversation_id,
                                  'turns': session.turns, 'claim': session.claim.__dict__,
                                  'verified': session.verified, 'device': session.device,
                                  'handed_off': session.handed_off, 'handoff_reason': session.handoff_reason,
                                  'latency_seconds': time.perf_counter() - start, 'events': evidence})
    st.close()
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print(json.dumps({'output': str(output), 'journeys': [{k:v for k,v in j.items() if k not in ('events', 'device', 'claim')} for j in result['journeys']]}, indent=2))
