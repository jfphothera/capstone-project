"""WATCH-LAB on the Capstone laptop: a live, read-only narration of what Capstone's agents are doing.

Sources (every database opened read-only, URI mode=ro; nothing is written, nothing is called):
  - each running Testing Lab trial: <data>\\experiments\\EXP-*\\...\\RUN-*\\operations.db, found by its worker-lease.db
    (a trial is running while its lease is in the future);
  - the live database <data>\\operations.db (cases worked in Capstone's own screens).
Per database it follows, by rowid, the tables Capstone already writes as it works:
  activity_events     each step as one line (intake, identity, safety, QMS procedures, tools, decisions, sends)
  retrieved_sources   RAG: every candidate passage, its similarity, selected or rejected and why
  inference_calls     every model call when it finishes: role, model, head, time, tokens, and its full written output
  audit_events        rule checks and decisions (a draft rejected and why, a send blocked)
  messages            each customer reply drafted, and its status

Models run with thinking off (Capstone sends think:false), so there is no private reasoning to show: what appears is
what each step wrote. A step's output appears when its call finishes. Output cut off by the call's output limit is
marked "CUT OFF".

  py -3 watch_capstone.py                    every running trial, plus the live database
  py -3 watch_capstone.py --run RUN-35e1     follow one trial (any unique part of its id)
  py -3 watch_capstone.py --replay PATH.db   replay a recorded trial database (rehearsals, tests)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

ROLE_NAMES = {
    "ticket_interpretation": "Ticket interpretation", "erp_research": "ERP research", "qms_research": "QMS research",
    "resolution_planning": "Planner", "communication_review": "Communication review", "customer_communication": "Writer",
    "independent_validation": "Validator", "supervisor": "Supervisor", "reflection": "Reflection",
    "peer_reviewer": "Peer reviewer", "tool_delegate": "Tool delegate", "investigation": "Investigation",
    "retrieval_embedding": "Retrieval (embedding)", "coordinator": "Coordinator", "lab_reviewer": "Reviewer (simulated human)",
    "lab-customer-simulator": "Simulated customer", "lab-environment-simulator": "Simulated environment",
    "testing_lab_probe": "Testing Lab probe", "safety_complaint": "Safety and complaints",
}
SKIP_ACTIVITY = {"inference", "model"}        # shown as the model-call block instead (with its output)
QUIET_ACTIVITY = {"embedding", "configuration", "methodology"}

C = {"dim": "\033[90m", "step": "\033[37m", "rag": "\033[36m", "model": "\033[33m", "out": "\033[97m",
     "rule": "\033[35m", "send": "\033[32m", "bad": "\033[31m", "head": "\033[94m", "end": "\033[0m"}


def paint(kind, text, color=True):
    return f"{C[kind]}{text}{C['end']}" if color else text


def connect(path):
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=1.0)
    db.row_factory = sqlite3.Row
    return db


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def clock(value):
    """'HH:MM:SS' local time from an ISO timestamp or epoch seconds."""
    try:
        t = datetime.fromtimestamp(float(value), timezone.utc) if isinstance(value, (int, float)) or str(value).replace(".", "").isdigit() \
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError, OSError):
        return "--:--:--"


def epoch(value):
    """Seconds since 1970 from an ISO timestamp or epoch seconds (the tables use both), for ordering."""
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "").isdigit():
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def head_name(endpoint):
    e = str(endpoint or "")
    if e.startswith("head-192-0-2-"):
        return "head " + e.rsplit("-", 1)[1]
    return e or "?"


def wrap(text, indent, width=110):
    return textwrap.fill(" ".join(str(text).split()), width=width, initial_indent=indent, subsequent_indent=indent)


def render_output(payload, color=True):
    """The written output of one model call, as indented lines."""
    resp = payload.get("response") or {}
    msg = resp.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    content = content if content else resp.get("response")
    lines = []
    calls = msg.get("tool_calls") if isinstance(msg, dict) else None
    for call in calls or []:
        fn = (call or {}).get("function") or {}
        lines.append(wrap(f"tool chosen: {fn.get('name')}  arguments: {json.dumps(fn.get('arguments'), ensure_ascii=False)[:300]}", "      "))
    if isinstance(resp.get("embeddings"), list) or isinstance(resp.get("embedding"), list):
        lines.append("      (an embedding: numbers for the search index, no text)")
        return lines
    if content:
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            value = content
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, list):
                    if not v:
                        lines.append(f"      {k}: (none)")
                    for i, item in enumerate(v[:6], 1):
                        lines.append(wrap(f"{k} {i}: {json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item}", "      "))
                else:
                    lines.append(wrap(f"{k}: {v}", "      "))
        else:
            text = str(value).strip()
            lines += [wrap(part, "      ") for part in text.splitlines()[:10] if part.strip()]
            if len(text.splitlines()) > 10:
                lines.append("      …")
    if not lines:
        lines.append("      (no text returned)")
    return [paint("out", l, color) for l in lines]


class Source:
    """One Capstone database being followed."""
    FOLLOW = ("activity_events", "retrieved_sources", "inference_calls", "audit_events", "messages")

    def __init__(self, path, label, from_start):
        self.path, self.label, self.cursor, self.from_start, self.missing = Path(path), label, {}, from_start, 0
        self.short = label.split(" (")[0]          # each line carries the short id; the full label is printed once

    def poll(self, color=True):
        out = []
        try:
            db = connect(self.path)
        except sqlite3.Error:
            self.missing += 1
            return out
        try:
            have = tables(db)
            for table in self.FOLLOW:
                if table not in have:
                    continue
                if table not in self.cursor:
                    top = db.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
                    self.cursor[table] = 0 if self.from_start else top
                rows = db.execute(f"SELECT rowid AS _r, * FROM {table} WHERE rowid>? ORDER BY rowid LIMIT 400",
                                  (self.cursor[table],)).fetchall()
                if rows:
                    self.cursor[table] = rows[-1]["_r"]
                out += getattr(self, "_" + table)(rows, color)
        except sqlite3.Error as exc:                  # busy for a moment: try again on the next pass
            out.append(paint("dim", f"   ({self.label}: database busy, {exc.__class__.__name__}; retrying)", color))
        finally:
            db.close()
        out.sort(key=lambda x: x[0])
        return out

    def _tag(self, case=None):
        return f"{self.short}{' ' + case if case else ''}"

    def _activity_events(self, rows, color):
        out = []
        for r in rows:
            cat = r["category"] or ""
            if cat in SKIP_ACTIVITY:
                continue
            kind = "dim" if cat in QUIET_ACTIVITY else "rule" if cat in ("identity", "verification", "authorization", "safety", "consistency") \
                else "send" if cat in ("response", "message") else "bad" if r["status"] in ("failed", "blocked") else "step"
            status = "" if r["status"] in ("completed", None) else f" [{r['status']}]"
            out.append((epoch(r["at"]), paint(kind, f"{clock(r['at'])}  {self._tag(r['case_id'])}  {cat}: {r['summary']}{status}", color)))
        return out

    def _retrieved_sources(self, rows, color):
        groups = {}
        for r in rows:
            groups.setdefault((r["at"], r["case_id"]), []).append(r)
        out = []
        for (at, case), items in groups.items():
            kept = [i for i in items if str(i["status"]).upper() != "REJECT"]
            head = f"{clock(at)}  {self._tag(case)}  RAG: {len(items)} candidate passage(s), {len(kept)} kept"
            lines = [paint("rag", head, color)]
            for i in sorted(kept, key=lambda i: int(i["rank"] or 999))[:5]:
                lines.append(paint("rag", f"      kept   {i['chunk_id']}  similarity {i['similarity']}  ({i['selected_by']})", color))
            rejected = [i for i in items if i not in kept]
            if rejected:
                best = max(rejected, key=lambda i: float(i["similarity"] or 0))
                lines.append(paint("dim", wrap(f"rejected {len(rejected)}; best rejected {best['chunk_id']} ({best['similarity']}): {best['reason']}", "      "), color))
            out.append((epoch(at), "\n".join(lines)))
        return out

    def _inference_calls(self, rows, color):
        out = []
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except (ValueError, TypeError):
                p = {}
            role = p.get("route_role") or r["role"]
            t = p.get("timings") or {}
            secs = (p.get("duration_ms") or 0) / 1000
            tokens = t.get("eval_count")
            cut = t.get("done_reason") == "length"
            ok = r["status"] == "completed"
            line = (f"{clock(r['at'])}  {self._tag(r['case_id'])}  MODEL {ROLE_NAMES.get(role, role)}: {r['model']} on "
                    f"{head_name(p.get('endpoint_id') or r['endpoint_id'])}, {secs:.1f} s"
                    + (f", {tokens} tokens" if tokens is not None else "")
                    + (f", waited {p['slot_wait_ms'] / 1000:.1f} s for a slot" if p.get("slot_wait_ms") else ""))
            block = [paint("model" if ok else "bad", line + ("" if ok else f"  FAILED: {str(p.get('error'))[:160]}"), color)]
            if cut:
                block.append(paint("bad", f"      CUT OFF: the answer reached its output limit ({p.get('requested_num_predict') or (p.get('effective_settings') or {}).get('num_predict')} tokens)", color))
            if ok:
                block += render_output(p, color)
            out.append((epoch(r["at"]), "\n".join(block)))
        return out

    def _audit_events(self, rows, color):
        out = []
        for r in rows:
            bad = any(w in (r["event_type"] or "") for w in ("rejected", "blocked", "denied", "failed", "unresolved"))
            out.append((epoch(r["at"]), paint("bad" if bad else "rule",
                        f"{clock(r['at'])}  {self._tag(r['case_id'])}  RULE {r['event_type']} ({r['actor']}): {r['summary']}", color)))
        return out

    def _messages(self, rows, color):
        out = []
        for r in rows:
            if r["direction"] != "outbound":
                head = f"{clock(r['created_at'])}  {self._tag(r['case_id'])}  CUSTOMER wrote: {r['subject'] or ''}"
                out.append((epoch(r["created_at"]), paint("step", head, color) + "\n" + paint("out", wrap(str(r["body"] or "")[:400], "      "), color)))
                continue
            head = f"{clock(r['created_at'])}  {self._tag(r['case_id'])}  DRAFT {r['message_id']} by {r['author']} [{r['status']}]"
            body = [wrap(l, "      ") for l in str(r["body"] or "").splitlines()[:8] if l.strip()]
            out.append((epoch(r["created_at"]), "\n".join([paint("send", head, color)] + [paint("out", b, color) for b in body])))
        return out


def lease_open(lease_db):
    try:
        db = sqlite3.connect(f"file:{Path(lease_db).as_posix()}?mode=ro", uri=True, timeout=0.5)
        row = db.execute("SELECT expires FROM lease WHERE id=1").fetchone()
        db.close()
        return bool(row) and row[0] > time.time() - 15
    except sqlite3.Error:
        return False


def trial_label(run_dir):
    label = run_dir.name[:12]
    for name in ("manifest.json", "result.json"):
        try:
            m = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run = m.get("run") if isinstance(m.get("run"), dict) else m
        bits = [str(run.get(k)) for k in ("method", "main_model") if run.get(k)]
        scen = run.get("scenario")
        if isinstance(scen, dict):
            scen = scen.get("id") or scen.get("name")
        if scen:
            bits.append(str(scen)[:28])
        if bits:
            return f"{label} ({', '.join(bits)})"
    return label


def discover(data, follow, known, started):
    """Running trials (and the live database) not followed yet."""
    found = []
    live = data / "operations.db"
    if not follow and live.exists() and str(live) not in known:
        found.append(Source(live, "LIVE", False))
    exp = data / "experiments"
    for lease in exp.glob("EXP-*/**/worker-lease.db"):
        run_dir = lease.parent
        db = run_dir / "operations.db"
        if str(db) in known or not db.exists() or (follow and follow not in run_dir.name):
            continue
        if lease_open(lease):
            # A trial that started after the window opened is shown from its start; one already running, from now.
            new = db.stat().st_ctime > started - 5
            found.append(Source(db, trial_label(run_dir), new))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=os.environ.get("CAPSTONE_DATA_ROOT", r"C:\ProgramData\Capstone"))
    ap.add_argument("--run", default="", help="follow one trial: any unique part of its RUN- id")
    ap.add_argument("--replay", default="", help="replay a recorded trial database (rehearsals and tests)")
    ap.add_argument("--speed", type=float, default=0.6, help="seconds between replayed lines")
    ap.add_argument("--once", action="store_true", help="one pass, then stop (tests)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    color = not args.no_color
    if color and os.name == "nt":
        os.system("")                                  # turn on the console's colour codes
    print(paint("head", "WATCH-LAB: Capstone's agents, live. Read-only: nothing here changes or slows a run.", color))
    print(paint("dim", "Models run with thinking off: each step's written output appears when its model call finishes.", color))
    if args.replay:
        src = Source(args.replay, "REPLAY " + Path(args.replay).parent.name[:12], True)
        for _, text in src.poll(color):
            print(text, flush=True)
            if not args.once:
                time.sleep(args.speed)
        return 0
    data = Path(args.root) / "data"
    if not data.exists():
        print(paint("bad", f"Capstone's data folder is not at {data}. Is this the Capstone laptop?", color))
        return 2
    sources, known, started, last_scan = [], set(), time.time(), 0.0
    while True:
        if time.time() - last_scan > 5:
            for s in discover(data, args.run, known, started):
                known.add(str(s.path))
                sources.append(s)
                print(paint("head", f"--- now following {s.label}" + ("  (from its start)" if s.from_start else "  (from now)"), color), flush=True)
            last_scan = time.time()
            if not sources:
                print(paint("dim", f"{datetime.now():%H:%M:%S}  no trial running yet; waiting (Ctrl+C to close)", color), flush=True)
        lines = []
        for s in list(sources):
            lines += s.poll(color)
            if s.label != "LIVE" and s.missing > 5:
                sources.remove(s)
        lines.sort(key=lambda x: x[0])
        for _, text in lines:
            print(text, flush=True)
        if args.once:
            return 0
        time.sleep(1.5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nWATCH-LAB closed.")
