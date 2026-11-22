"""Gateway routing check (GATEWAY-ROUTING-CHECK.cmd): why does the Inference Gateway send every request to one head?

READ-ONLY. It changes, restarts, reconfigures, re-qualifies nothing and changes no ACL or permission.
  Part A: the published observation file (Arena's view): one full read (A1), then a sample every 5 s for 5 min (A2).
  Part B: the Gateway's ledger, opened read-only (mode=ro), only when this process is elevated (Administrator).
          No query selects token_hash or any other credential column.
Results: gateway-routing-<computer>-<date>\report.txt (and samples.jsonl, observation-A1.json) next to this file.
"""
import argparse
import ctypes
import datetime as dt
import getpass
import json
import os
import socket
import sqlite3
import sys
import time

DEFAULT_OBS = r"C:\ProgramData\InferenceGateway\observation\shared-inference-observation.json"
DEFAULT_LEDGER = r"C:\ProgramData\InferenceGateway\state\capacity-ledger.db"
WATCH_EVENTS = ("resource.quarantined", "resource.health_changed", "qualification.invalidated", "lease.expired")

B_QUERIES = [
    ("B1. Order the gateway checks heads in",
     "SELECT rowid, resource_id, resource_type, health, datetime(last_seen,'unixepoch','localtime'), qualified_models, "
     "applications, permitted_uses FROM capacity_resources ORDER BY rowid;"),
    ("B2. Pinned vs dynamic placement and where work went",
     "SELECT resource_id, model, node_policy, status, count(*) FROM capacity_requests WHERE resource_id IS NOT NULL "
     "GROUP BY 1,2,3,4 ORDER BY 5 DESC;"),
    ("B3. Last 50 requests",
     "SELECT app_id, model, node_policy, nodes, status, resource_id, datetime(created,'unixepoch','localtime') "
     "FROM capacity_requests ORDER BY created DESC LIMIT 50;"),
    ("B4. Qualified context limit per head/model/purpose",
     "SELECT resource_id, model, passed, datetime(at,'unixepoch','localtime'), json_extract(evidence,'$.purpose'), "
     "json_extract(evidence,'$.execution_policy.max_context') FROM capacity_qualification_evidence "
     "ORDER BY resource_id, model, at DESC;"),
]
# Extra read-only queries (not in the request; labelled as extra). Still no credential column.
B_EXTRA = [
    ("B5 (extra). Requests created during the A2 sampling window, by head",
     "SELECT resource_id, app_id, model, node_policy, nodes, status, count(*) FROM capacity_requests "
     "WHERE created >= ? AND created <= ? GROUP BY 1,2,3,4,5,6 ORDER BY 7 DESC;"),
    ("B6 (extra). Application limits (no token_hash)",
     "SELECT c.app_id, c.enabled, c.max_concurrency, c.queue_limit, c.priority, c.models, l.operations, l.max_context "
     "FROM capacity_clients c LEFT JOIN capacity_client_limits l ON l.app_id = c.app_id ORDER BY c.app_id;"),
    # The observation file's events carry no head; the ledger's payload says which head changed and how.
    ("B7 (extra). Last 100 quarantine / health / invalidation / lease-expiry events with their payload",
     "SELECT event_id, datetime(at,'unixepoch','localtime'), app_id, request_id, event_type, substr(payload,1,400) "
     "FROM capacity_events WHERE event_type IN ('resource.quarantined','resource.health_changed',"
     "'qualification.invalidated','lease.expired') ORDER BY event_id DESC LIMIT 100;"),
]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def local(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


class Report:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8", newline="\r\n")

    def line(self, text=""):
        print(text)
        self.f.write(text + "\n")
        self.f.flush()

    def head(self, text):
        self.line("")
        self.line("=" * 100)
        self.line(text)
        self.line("=" * 100)


def read_obs(path):
    """One read of the observation file; None when it is missing, mid-replace or not valid JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return None, "%s: %s" % (type(e).__name__, e)


def observation_path(r):
    """Arena's aes.config.json gateway.observation_file, when one is readable and set; otherwise the default."""
    found = []
    base = r"C:\Users"
    try:
        users = os.listdir(base)
    except OSError:
        users = []
    for u in users:
        p = os.path.join(base, u, "AppData", "Local", "AgentArenaService", "aes.config.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            found.append((u, p, (cfg.get("gateway") or {}).get("observation_file")))
        except (OSError, ValueError) as e:
            r.line("  could not read %s: %s (no access is changed to read it)" % (p, e))
    for u, p, v in found:
        r.line("  %s: gateway.observation_file = %s" % (p, v))
    pick = next((v for u, p, v in found if u.lower() == "arena_service" and v), None) or next((v for u, p, v in found if v), None)
    if not found:
        r.line("  no readable aes.config.json found; using the default path")
    if pick and os.path.normcase(pick) != os.path.normcase(DEFAULT_OBS):
        r.line("  Arena's setting differs from the default: using %s" % pick)
    return pick or DEFAULT_OBS


def part_a1(r, obs, raw_dir):
    r.head("PART A1. One read of the published observation file")
    now = time.time()
    r.line("current local time : %s (epoch %.3f)" % (local(now), now))
    r.line("observed_at        : %s  (%s local, %.1f s before now)" % (obs.get("observed_at"), local(obs.get("observed_at")), now - float(obs.get("observed_at") or 0)))
    r.line("admission_open     : %s" % obs.get("admission_open"))
    r.line("ledger_id          : %s" % obs.get("ledger_id"))
    r.line("contract           : %s" % obs.get("contract"))
    with open(os.path.join(raw_dir, "observation-A1.json"), "w", encoding="utf-8") as f:
        json.dump(obs, f, indent=1)

    r.line("")
    r.line("resources[] (in file order): resource_id | resource_type | health | last_seen | fresh | qualified_models")
    for x in obs.get("resources") or []:
        r.line("  %s | %s | %s | %s (%s) | %s | %s" % (x.get("resource_id"), x.get("resource_type"), x.get("health"),
                                                       x.get("last_seen"), local(x.get("last_seen")), x.get("fresh"),
                                                       json.dumps(x.get("qualified_models"))))
    r.line("")
    if "inventory" not in obs:
        r.line("inventory[]: NOT IN THE FILE")
    r.line("inventory[]: resource_id | fresh | runtime_version | models (name digest)")
    for x in obs.get("inventory") or []:
        r.line("  %s | %s | %s | observed_at %s" % (x.get("resource_id"), x.get("fresh"), x.get("runtime_version"), local(x.get("observed_at"))))
        for m in x.get("models") or []:
            r.line("      %s  %s" % (m.get("name"), m.get("digest")))
    r.line("")
    r.line("applications[]: app_id | enabled | max_concurrency | queue_limit | priority")
    for x in obs.get("applications") or []:
        r.line("  %s | %s | %s | %s | %s" % (x.get("app_id"), x.get("enabled"), x.get("max_concurrency"), x.get("queue_limit"), x.get("priority")))
    r.line("")
    r.line("last_hour (everything in it):")
    r.line("  " + (json.dumps(obs["last_hour"], indent=1).replace("\n", "\n  ") if "last_hour" in obs else "NOT IN THE FILE"))
    r.line("")
    r.line("requests (counts): %s" % json.dumps(obs.get("requests")))
    r.line("invocations (counts): %s" % json.dumps(obs.get("invocations")))
    r.line("leases[] in this read: %d" % len(obs.get("leases") or []))
    for x in obs.get("leases") or []:
        r.line("  %s | %s | %s | %s" % (x.get("request_id"), x.get("app_id"), x.get("resource_id"), x.get("status")))
    r.line("")
    latest = {}
    for q in obs.get("qualification_observations") or []:
        k = (q.get("resource_id"), q.get("model"))
        if k not in latest or float(q.get("at") or 0) > float(latest[k].get("at") or 0):
            latest[k] = q
    r.line("qualification_observations, latest per resource+model (%d entries in the file): resource_id | model | passed | at" % len(obs.get("qualification_observations") or []))
    for k in sorted(latest, key=lambda k: (str(k[0]), str(k[1]))):
        q = latest[k]
        r.line("  %s | %s | %s | %s (%s)" % (k[0], k[1], q.get("passed"), q.get("at"), local(q.get("at"))))
    r.line("")
    ev = obs.get("events") or []
    r.line("events of type %s (the file holds the last %d events, ids %s to %s):" % (
        ", ".join(WATCH_EVENTS), len(ev), ev[0].get("event_id") if ev else "-", ev[-1].get("event_id") if ev else "-"))
    hits = [e for e in ev if e.get("event_type") in WATCH_EVENTS]
    for e in hits:
        r.line("  " + json.dumps(e) + "   (%s)" % local(e.get("at")))
    if not hits:
        r.line("  none")


def part_a2(r, path, samples, interval, raw_dir):
    r.head("PART A2. Leases every %g s for %d samples (%.0f s)" % (interval, samples, samples * interval))
    r.line("Each line: sample # | local time read | observed_at (local) | leases: request_id app_id resource_id status")
    log = open(os.path.join(raw_dir, "samples.jsonl"), "w", encoding="utf-8")
    seen = {}            # request_id -> set(resource_id)
    by_res = {}          # resource_id -> set(request_id)
    max_sim, max_at = 0, None
    multi = []           # samples with leases on two or more different heads
    skipped, stale_repeat, last_obs_at = 0, 0, None
    events = {}
    t0 = time.time()
    for i in range(samples):
        due = t0 + i * interval
        if time.time() < due:
            time.sleep(due - time.time())
        obs, err = read_obs(path)
        read_at = time.time()
        if obs is None:
            skipped += 1
            r.line("  %3d | %s | skipped: %s" % (i + 1, local(read_at), err))
            continue
        leases = obs.get("leases") or []
        log.write(json.dumps({"sample": i + 1, "read_at": read_at, "observed_at": obs.get("observed_at"), "leases": leases}) + "\n")
        log.flush()
        if obs.get("observed_at") == last_obs_at:
            stale_repeat += 1
        last_obs_at = obs.get("observed_at")
        parts = ["%s %s %s %s" % (x.get("request_id"), x.get("app_id"), x.get("resource_id"), x.get("status")) for x in leases]
        r.line("  %3d | %s | %s (%s) | %d lease(s)%s" % (i + 1, local(read_at), obs.get("observed_at"), local(obs.get("observed_at")),
                                                       len(leases), (": " + " ; ".join(parts)) if parts else ""))
        for x in leases:
            seen.setdefault(x.get("request_id"), set()).add(x.get("resource_id"))
            by_res.setdefault(x.get("resource_id"), set()).add(x.get("request_id"))
        if len(leases) > max_sim:
            max_sim, max_at = len(leases), local(obs.get("observed_at"))
        heads = {x.get("resource_id") for x in leases}
        if len(heads) >= 2:
            multi.append((i + 1, local(obs.get("observed_at")), sorted(map(str, heads))))
        for e in obs.get("events") or []:
            if e.get("event_type") in WATCH_EVENTS:
                events[e.get("event_id")] = e
    log.close()
    t1 = time.time()
    r.line("")
    r.line("A2 summary")
    r.line("  samples taken: %d, skipped (missing or mid-replace): %d, same observed_at as the sample before: %d" % (samples - skipped, skipped, stale_repeat))
    r.line("  distinct requests seen per resource_id:")
    for res in sorted(by_res, key=str):
        r.line("    %s : %d" % (res, len(by_res[res])))
    if not by_res:
        r.line("    none: no lease was seen in any sample (was Capstone sending work during these 5 minutes?)")
    r.line("  maximum simultaneous leases in one sample: %d%s" % (max_sim, (" (at %s)" % max_at) if max_at else ""))
    if multi:
        r.line("  two or more leases active at the same time on different heads: YES, in %d sample(s):" % len(multi))
        for s in multi:
            r.line("    sample %d at %s: %s" % s)
    else:
        r.line("  two or more leases active at the same time on different heads: NO (never in one sample)")
    moved = {k: v for k, v in seen.items() if len(v) > 1}
    if moved:
        r.line("  requests seen on more than one head across samples: %s" % json.dumps({k: sorted(map(str, v)) for k, v in moved.items()}))
    r.line("  %s events seen in any sample's events[] during the window:" % "/".join(WATCH_EVENTS))
    for k in sorted(events, key=lambda k: (k is None, k)):
        r.line("    " + json.dumps(events[k]) + "   (%s)" % local(events[k].get("at")))
    if not events:
        r.line("    none")
    return t0, t1


def fmt(v):
    return "NULL" if v is None else str(v)


def part_b(r, ledger, window):
    r.head("PART B. Gateway ledger, opened read-only")
    if not is_admin():
        r.line("SKIPPED: this window is not elevated (not 'Run as administrator'). Part B needs an elevated Administrator")
        r.line("session on 192.0.2.254. No access was granted to anyone to get around this.")
        return
    r.line("ledger: %s" % ledger)
    if not os.path.exists(ledger):
        r.line("SKIPPED: the ledger file is not there (or this account cannot see it).")
        return
    try:
        db = sqlite3.connect("file:" + ledger.replace("\\", "/") + "?mode=ro", uri=True, timeout=30)
        db.execute("PRAGMA query_only = ON")
    except sqlite3.Error as e:
        r.line("SKIPPED: could not open the ledger read-only: %s" % e)
        return
    for title, sql in B_QUERIES:
        run_query(r, db, title, sql, ())
    for title, sql in B_EXTRA:
        params = window if "?" in sql else ()
        if "?" in sql:
            title += " %s to %s" % (local(window[0]), local(window[1]))
        run_query(r, db, title, sql, params)
    db.close()


def run_query(r, db, title, sql, params):
    r.line("")
    r.line("-- " + title)
    r.line(sql)
    try:
        cur = db.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except sqlite3.Error as e:
        r.line("ERROR (table or column missing?): %s" % e)
        return
    r.line(" | ".join(cols))
    for row in rows:
        r.line(" | ".join(fmt(v) for v in row))
    r.line("(%d row(s))" % len(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default="")             # tests only: another observation file
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(a.out, "gateway-routing-%s-%s" % (os.environ.get("COMPUTERNAME", socket.gethostname()), stamp))
    os.makedirs(out, exist_ok=True)
    r = Report(os.path.join(out, "report.txt"))
    r.line("Gateway routing check (read-only). Nothing on this computer is changed.")
    r.line("computer %s | account %s | elevated %s | started %s" % (os.environ.get("COMPUTERNAME"), getpass.getuser(), is_admin(), local(time.time())))
    try:
        addrs = sorted({i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
    except OSError:
        addrs = []
    r.line("addresses: %s%s" % (", ".join(addrs), "" if "192.0.2.254" in addrs else "   (192.0.2.254 is not one of them: is this the hub?)"))
    r.line("results folder: %s" % out)
    if not is_admin():
        r.line("")
        r.line("NOTE: not elevated. Part A runs; Part B (the ledger) will be SKIPPED.")

    r.head("Which observation file")
    path = a.obs or observation_path(r)
    r.line("  reading: %s" % path)
    obs, err = read_obs(path)
    if obs is None:
        # one retry: the file is replaced atomically and a read can land mid-replace
        time.sleep(1)
        obs, err = read_obs(path)
    if obs is None:
        r.line("  could not read it: %s" % err)
        r.line("  Part A stopped here (no access was changed to get around it).")
        window = (time.time(), time.time())
    else:
        part_a1(r, obs, out)
        r.line("")
        r.line(">>> Sampling for %.0f seconds now. Capstone should be sending work during this time. <<<" % (a.samples * a.interval))
        window = part_a2(r, path, a.samples, a.interval, out)
    part_b(r, a.ledger, window)
    r.line("")
    r.line("Finished %s. Bring back the folder: %s" % (local(time.time()), out))


if __name__ == "__main__":
    main()
