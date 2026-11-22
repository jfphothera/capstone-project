"""Coder Onboarding: Coder's own Inference Gateway login, least privilege, through the Gateway's own tools.

    "C:\\Program Files\\Python312\\python.exe" -I coder_onboarding.py      (the "Coder Onboarding" shortcut, as LabOperator1)

A menu: 1 Inspect (read-only)   2 Set up Coder's Gateway login   3 Replace Coder's token (rotate)
        4 Delete the hub copy of the token (after it reached the Coder laptop)   5 Quit

Inspect reads the live Gateway (credentials list, observe, the heads each project uses), checks that nothing answers
on Coder's lab address 192.0.2.202 yet (ping, then the hub's ARP table) and lists the chat models that are qualified
now on a healthy head, coding models first. It changes nothing.

Set up, after APPROVE, in this order and only for `coder`:
  1. `credentials issue` with a policy that names only `coder`: one model (already installed and qualified, never
     downloaded), generate and chat only, at most one request at a time, a small queue, a context limit below the
     qualified one. The token is written once to C:\\ProgramData\\AgentArenaLab\\gateway\\outbox\\coder\\coder.token
     (SYSTEM and Administrators only) and is never shown or read by this tool;
  2. the heads where that model is qualified serve `coder` too (the Gateway's heads tool, then bootstrap, as the Fleet
     Commit does): every project a head serves stays, qualifications are kept;
  3. the hub's copy of the approved policy and the Fleet window's state are brought up to date.
No other project's models, limits or heads change. The Gateway Models screen cannot widen Coder's models
("Available to all" never reaches it, gateway_models.EXPLICIT_ONLY); the Fleet window's Allow is the only way to add one.

A non-secret receipt (what the Gateway holds for coder, the model, its digest, the heads, the evidence) is written to
C:\\ProgramData\\AgentArenaLab\\coder-onboarding and, when the provisioner stick is plugged in, to its reports folder.

Arena is not touched here: add the application `coder` and reserve 192.0.2.202 in the admin hub (ArenaSvcUser's
screen). Coder's Arena app code is issued later, when Coder's own publisher can redeem it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import string
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
APP = "coder"
ADDRESS = "192.0.2.202"
DATA = Path(r"C:\ProgramData\AgentArenaLab\coder-onboarding")
OUTBOX = Path(r"C:\ProgramData\AgentArenaLab\gateway\outbox\coder")
TOKEN_NAME = "coder.token"
CONTRACT = "shared.inference/1"
# Least privilege for a first, single-user desktop client. Priority 5 is the Gateway's own template value for coder
# (config/policy.template.json): below operational Capstone (6), above Capstone research (2) and lab traffic (1).
OPERATIONS = ["/api/generate", "/api/chat"]
MAX_CONCURRENCY = 1
QUEUE_LIMIT = 4
PRIORITY = 5
MAX_CONTEXT = 4096
CODING = re.compile(r"(?i)coder|codestral|codellama|starcoder|devstral")
MAC = re.compile(r"(?i)\b([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\b")


def _load(name, folder=HERE):
    path = next((p for p in (folder / f"{name}.py", folder.parent / "model-library" / f"{name}.py") if p.is_file()), folder / f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------- pure helpers (tested)

def candidates(snapshot, *, is_chat, healthy_only=True):
    """Chat models qualified right now, one entry per (model, digest), coding models first. Pure.

    A head counts only if the Gateway reports it healthy, its inventory is fresh, the model is on its qualified list,
    the newest qualification of that model there passed, and that test was of the digest the head has installed now.
    """
    resources = {r["resource_id"]: r for r in snapshot.get("resources", []) if r.get("resource_type") == "shared_inference"}
    inventory = {i["resource_id"]: i for i in snapshot.get("inventory", [])}
    latest = {}
    for q in sorted(snapshot.get("qualification_observations") or [], key=lambda q: q.get("at") or 0):
        latest[(q.get("resource_id"), q.get("model"))] = q
    groups = {}
    for rid, r in resources.items():
        if healthy_only and r.get("health") != "healthy":
            continue
        inv = inventory.get(rid) or {}
        if healthy_only and not inv.get("fresh"):
            continue
        installed = {m.get("name"): m.get("digest") for m in inv.get("models", [])}
        for model in r.get("qualified_models") or []:
            if not is_chat(model):
                continue
            q = latest.get((rid, model))
            digest = installed.get(model)
            if not q or not q.get("passed") or not digest or q.get("digest") != digest:
                continue
            g = groups.setdefault((model, digest), {"model": model, "digest": digest, "coding": bool(CODING.search(model)),
                                                    "heads": [], "evidence": {}, "runtime": {}})
            g["heads"].append(rid)
            g["evidence"][rid] = {"evidence_id": q.get("evidence_id"), "at": q.get("at")}
            g["runtime"][rid] = inv.get("runtime_version")
    out = list(groups.values())
    for g in out:
        g["heads"].sort(key=lambda rid: int(rid.rsplit("-", 1)[1]))
    return sorted(out, key=lambda g: (not g["coding"], -len(g["heads"]), g["model"], g["digest"]))


def coder_policy(model):
    """The policy document `credentials issue` reads for coder: coder only, one model. Pure."""
    if not isinstance(model, str) or not model:
        raise ValueError("choose one model")
    return {"configuration_version": 1, "contract": CONTRACT,
            "clients": [{"app_id": APP, "credential": "provisioned", "models": [model], "max_concurrency": MAX_CONCURRENCY,
                         "queue_limit": QUEUE_LIMIT, "priority": PRIORITY, "operations": list(OPERATIONS),
                         "max_context": MAX_CONTEXT}],
            "resources": []}


def head_changes(head_apps, heads):
    """Fleet Commit `head-projects` changes that add coder to these heads and keep every project they serve. Pure."""
    out = []
    for rid in heads:
        have = head_apps.get(rid)
        if have is None:
            raise ValueError(f"{rid} is not a Gateway head")
        if APP in have:
            continue
        address = "192.0.2." + rid.rsplit("-", 1)[1]
        out.append({"op": "head-projects", "address": address, "applications": sorted(set(have) | {APP})})
    return out


def arp_verdict(arp_text, address=ADDRESS):
    """'in-use' with the MAC when the hub's ARP table holds a hardware address for it, else 'no-answer'. Pure."""
    for line in (arp_text or "").splitlines():
        parts = line.split()
        if parts and parts[0] == address:
            m = MAC.search(line)
            if m and m.group(1).lower() not in ("ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00"):
                return {"verdict": "in-use", "mac": m.group(1).lower(), "line": line.strip()}
    return {"verdict": "no-answer", "mac": None, "line": None}


def coder_state(applications, head_apps):
    """What the Gateway holds for coder, without anything secret (the credentials list carries none). Pure."""
    a = next((x for x in applications if x.get("app_id") == APP), None)
    heads = sorted((rid for rid, apps in head_apps.items() if APP in apps), key=lambda rid: int(rid.rsplit("-", 1)[1]))
    if not a:
        return {"registered": False, "heads": heads}
    keep = ("enabled", "models", "max_concurrency", "queue_limit", "priority", "operations", "max_context")
    return {"registered": True, **{k: a.get(k) for k in keep}, "heads": heads}


def others_unchanged(before, after):
    """Every other project's models and limits are exactly as before. Pure."""
    def strip(apps):
        return {a["app_id"]: {k: v for k, v in a.items() if k != "app_id"} for a in apps if a.get("app_id") != APP}
    return strip(before) == strip(after)


def other_heads_unchanged(before, after):
    """Every project a head served before, it serves after (coder may be added). Pure."""
    return all(set(apps) <= set(after.get(rid, [])) and set(after.get(rid, [])) - set(apps) <= {APP}
               for rid, apps in before.items())


# ---------------------------------------------------------------------------- the hub

def _tools():
    gm = sys.modules.get("gateway_models") or _load("gateway_models")
    fc = sys.modules.get("fleet_commit") or _load("fleet_commit")
    args = argparse.Namespace(release=None, ledger=None, gateway_root=str(gm.GATEWAY_ROOT), gateway_data=str(gm.GATEWAY_DATA),
                              data=str(gm.CONSOLE_DATA), library=str(fc.LIBRARY_DIR), fleet_dir=str(fc.FLEET_DIR), hub=fc.HUB)
    return gm, fc, fc.build(args)


def _restrict(folder):
    folder.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/Q"],
                       capture_output=True, text=True, check=False)
    if r.returncode:
        raise RuntimeError(f"could not restrict {folder} to SYSTEM and Administrators: {r.stderr.strip() or r.stdout.strip()}")


def probe_address(address=ADDRESS):
    """Ping it (twice), then read the hub's ARP table: a device holding the address answers ARP even when it drops ping."""
    subprocess.run(["ping", "-n", "2", "-w", "800", address], capture_output=True, check=False)
    arp = subprocess.run(["arp", "-a", address], capture_output=True, text=True, check=False).stdout
    return arp_verdict(arp, address)


def stick_reports():
    """The provisioner stick's reports folder for this computer, if the stick is plugged in (any drive letter)."""
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if (root / "reports").is_dir() and any(root.glob("AGENT-ARENA-LAB-*")):
                return root / "reports" / os.environ.get("COMPUTERNAME", "hub")
        except OSError:
            continue
    return None


def inspect(gm, fc, fleet, say=print):
    apps = fleet.tools.applications()
    snap = fleet.tools.snapshot()
    head_apps = fc.ledger_heads(fleet.tools.ledger)
    state = coder_state(apps, head_apps)
    cands = candidates(snap, is_chat=gm.is_chat_model)
    address = probe_address()
    token = OUTBOX / TOKEN_NAME
    doc = {"schema": "agent-arena.coder-onboarding/1", "tool": f"Coder Onboarding {VERSION}", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "computer": os.environ.get("COMPUTERNAME"), "operator": fleet.tools.operator,
           "gateway": {"ledger_id": snap.get("ledger_id"), "service_id": snap.get("service_id"), "admission_open": snap.get("admission_open"),
                       "observed_at": snap.get("observed_at"), "release": fleet.tools.release.name},
           "applications": [{k: a.get(k) for k in ("app_id", "enabled", "max_concurrency", "queue_limit", "priority", "operations", "max_context")}
                            | {"models": len(a.get("models") or [])} for a in apps],
           "heads": {rid: apps_ for rid, apps_ in sorted(head_apps.items(), key=lambda kv: int(kv[0].rsplit("-", 1)[1]))},
           "coder": state, "address_check": {"address": ADDRESS, **address},
           "hub_token_copy": {"path": str(token), "present": token.is_file()},
           "candidates": cands[:12],
           "explicit_only_guard": sorted(getattr(gm, "EXPLICIT_ONLY", {})),
           "secrets_included": False}
    say(f"Gateway {doc['gateway']['release']} ledger {doc['gateway']['ledger_id']}: projects " +
        ", ".join(f"{a['app_id']} ({a['models']} models, {a['max_concurrency']} at a time)" for a in doc["applications"]))
    if state["registered"]:
        say(f"coder: REGISTERED, enabled {state['enabled']}; models {', '.join(state['models'] or [])}; {state['max_concurrency']} at a time, "
            f"queue {state['queue_limit']}, priority {state['priority']}; operations {', '.join(state['operations'] or [])}; "
            f"context {state['max_context']}; heads {', '.join(state['heads']) or 'none'}")
    else:
        say("coder: not in the Gateway (no login yet)")
    say(f"{ADDRESS}: " + ("IN USE: the hub's ARP table holds " + address["mac"] if address["verdict"] == "in-use"
                          else "nothing answered (no ARP entry): free as far as the lab cable shows"))
    say(f"hub copy of Coder's token: {'present' if token.is_file() else 'none'} ({token})")
    if cands:
        say("chat models qualified now on a healthy head (coding models first):")
        for n, c in enumerate(cands[:12], 1):
            say(f"  {n:2}. {c['model']:<44} digest {c['digest'][:12]}  heads {', '.join(h.replace('head-192-0-2-', '') for h in c['heads'])}"
                + ("  (coding model)" if c["coding"] else ""))
    else:
        say("no chat model is qualified on a healthy head right now")
    return doc, apps, head_apps, cands


def add_heads(gm, fleet, changes, say=print):
    """Fleet Commit's step 2 alone (the Gateway's heads tool, then bootstrap), for the heads in `changes`.

    Not fleet.run(): that also tests every allowed, not yet qualified model on each head it touches (other projects'
    models, minutes each, and a timed-out test locks the head). Coder's model is qualified there already."""
    failures = []
    addresses = [c["address"] for c in changes]
    scan = fleet.scanner(addresses) if addresses else {}
    for c in changes:
        address = c["address"]
        live = scan.get(address)
        if not live:
            failures.append(f"head {address}: it did not answer over the lab cable; nothing changed for it")
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ev = fleet.console.data / "evidence"
        ev.mkdir(parents=True, exist_ok=True)
        inv = ev / f"inventory-{address}-{stamp}.json"
        inv.write_text(json.dumps(gm.head_inventory(address, live["version"], [{"name": n, "digest": d} for n, d in live["models"].items()],
                                                    fleet.console.computer), indent=2), encoding="utf-8")
        base = ev / f"base-{stamp}.json"
        base.write_text(json.dumps({"configuration_version": 1, "contract": CONTRACT, "clients": [], "resources": []}), encoding="utf-8")
        review = ev / f"head-review-{address}-{stamp}.json"
        r = fleet.tools.heads(inv, base, c["applications"], review)
        if r["ok"]:
            r = fleet.tools.bootstrap(review)
        if r["ok"]:
            say(f"  head {address} serves: {', '.join(c['applications'])}")
            fleet.console.log({"action": "head projects (Coder Onboarding)", "address": address, "applications": c["applications"]})
        else:
            failures.append(f"head {address}: the Gateway refused it: " + (r["error"] or f"exit {r['exit']}"))
    return failures


def write_receipt(doc, kind):
    _restrict(DATA)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    text = json.dumps(doc, indent=2)
    if re.search(r"(?i)token_hash|x-infrastructure-token|ARENA-[A-Z0-9]{4}-", text):
        raise RuntimeError("refusing to write a receipt that looks like it holds a secret")
    paths = [DATA / f"{kind}-{stamp}.json"]
    paths[0].write_text(text, encoding="utf-8")
    rep = stick_reports()
    if rep:
        out = rep / "coder-onboarding" / f"{kind}-{stamp}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        paths.append(out)
    return paths


def setup(ask=input, say=print):
    gm, fc, fleet = _tools()
    if APP not in getattr(gm, "EXPLICIT_ONLY", {}):
        raise RuntimeError("this Gateway Models screen does not keep Coder's models explicit: install the stick's version "
                           "first (START-LAB-PROVISIONER > 3 > 5), then start Coder Onboarding from its new shortcut")
    doc, apps, head_apps, cands = inspect(gm, fc, fleet, say)
    if doc["coder"]["registered"]:
        say("coder already holds a Gateway login: nothing is issued again (3 replaces its token).")
        missing = [rid for c in cands if c["model"] in (doc["coder"]["models"] or []) for rid in c["heads"] if rid not in doc["coder"]["heads"]]
        if missing:
            say("heads where its model is qualified but that do not serve coder yet: " + ", ".join(missing))
        return doc
    if doc["address_check"]["verdict"] == "in-use":
        raise RuntimeError(f"{ADDRESS} is in use on the lab cable ({doc['address_check']['mac']}): STOPPED. Find that device first; "
                           "Coder's laptop never takes over an address another device holds.")
    if not cands:
        raise RuntimeError("no chat model is qualified on a healthy head: nothing to give Coder (nothing is downloaded here)")
    pick = ask("Model for Coder: type its number (Enter for 1): ").strip() or "1"
    if not pick.isdigit() or not 1 <= int(pick) <= min(12, len(cands)):
        raise RuntimeError("no model chosen; nothing changed")
    c = cands[int(pick) - 1]
    policy = coder_policy(c["model"])
    changes = head_changes(head_apps, c["heads"])
    say("")
    say(f"Coder's Gateway login, exactly:")
    say(f"  model        {c['model']}  (digest {c['digest']}){'' if c['coding'] else '  GENERAL-PURPOSE, not a coding model'}")
    say(f"  heads        {', '.join(c['heads'])} (qualified there now; each keeps every project it serves)")
    say(f"  operations   {', '.join(OPERATIONS)}; {MAX_CONCURRENCY} request at a time; queue {QUEUE_LIMIT}; priority {PRIORITY}; context at most {MAX_CONTEXT}")
    say(f"  output       the Gateway's own limit per request (8192 tokens; Gateway 0.3.0 has no per-project output limit)")
    say(f"  token        written once to {OUTBOX / TOKEN_NAME} (SYSTEM and Administrators); never shown")
    if ask("Type APPROVE to set this up (anything else stops; nothing changes): ").strip() != "APPROVE":
        say("stopped by the operator; nothing changed")
        return doc
    _restrict(DATA)
    _restrict(OUTBOX)
    token = OUTBOX / TOKEN_NAME
    if token.exists():
        raise RuntimeError(f"{token} already exists (an earlier attempt?): nothing issued. Delete it (4) or rotate (3).")
    pf = DATA / f"coder-policy-{time.strftime('%Y%m%d-%H%M%S')}.json"
    pf.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    r = fleet.tools.run("credentials", "issue", "--database", str(fleet.tools.ledger), "--app-id", APP, "--policy", str(pf), "--output", str(token))
    if not r["ok"]:
        raise RuntimeError("the Gateway refused Coder's login: " + (r["error"] or f"exit {r['exit']}") + " (nothing else was done)")
    say("  the Gateway issued Coder's login")
    for f in add_heads(gm, fleet, changes, say):
        say("  not done: " + f)
    after_apps = fleet.tools.applications()
    after_heads = fc.ledger_heads(fleet.tools.ledger)
    fleet.console._write_approved({a["app_id"]: a.get("models") or [] for a in after_apps}, after_apps)
    fleet.publish()
    snap = fleet.tools.snapshot()
    still = next((x for x in candidates(snap, is_chat=gm.is_chat_model) if x["model"] == c["model"] and x["digest"] == c["digest"]), None)
    result = {"schema": "agent-arena.coder-onboarding/1", "tool": f"Coder Onboarding {VERSION}", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "computer": os.environ.get("COMPUTERNAME"), "operator": fleet.tools.operator, "action": "setup",
              "coder": coder_state(after_apps, after_heads),
              "selected": {"model": c["model"], "digest": c["digest"], "coding_model": c["coding"], "evidence": c["evidence"],
                           "runtime": c["runtime"], "qualified_after": bool(still), "heads_after": (still or {}).get("heads", [])},
              "policy_sent": policy["clients"][0], "output_limit": "Gateway service limit 8192 tokens per request (no per-project limit in 0.3.0)",
              "other_projects_unchanged": others_unchanged(apps, after_apps), "other_heads_unchanged": other_heads_unchanged(head_apps, after_heads),
              "address_check": doc["address_check"], "hub_token_copy": {"path": str(token), "present": token.is_file()},
              "secrets_included": False}
    paths = write_receipt(result, "setup")
    ok = (result["coder"]["registered"] and result["coder"]["models"] == [c["model"]] and set(c["heads"]) <= set(result["coder"]["heads"])
          and result["other_projects_unchanged"] and result["other_heads_unchanged"] and result["selected"]["qualified_after"])
    say("")
    say(("DONE" if ok else "CHECK") + f": coder serves on {', '.join(result['coder']['heads']) or 'no head'} with {c['model']}; "
        f"other projects unchanged {result['other_projects_unchanged']}, their heads unchanged {result['other_heads_unchanged']}")
    say("Receipt (no secrets): " + "; ".join(str(p) for p in paths))
    say("")
    say("Next:")
    say(f"  1. Copy the folder {OUTBOX} to the SECOND stick (never the provisioner stick). Carry it to the Coder laptop.")
    say("  2. Admin hub (ArenaSvcUser's screen): Machines > Machines and codes... > Applications: add ID coder, name Coder")
    say(f"     (if it is not there). Addresses: role 'Application laptop', address {ADDRESS}, coder/main > Reserve and get code.")
    say("     Do NOT make an App code for coder yet: Coder has no Arena publisher to redeem it.")
    say("  3. Coder laptop: START-LAB-PROVISIONER > 5. Then, signed in as Coder's account: TEST-CODER-GATEWAY.")
    say("  4. Back here: 4 deletes the hub copy of the token once the Coder laptop holds it.")
    return result


def rotate(ask=input, say=print):
    gm, fc, fleet = _tools()
    apps = {a["app_id"] for a in fleet.tools.applications()}
    if APP not in apps:
        raise RuntimeError("coder has no Gateway login yet: use 2")
    say("Rotating replaces Coder's token: the one on the Coder laptop stops working at once and the new file must be carried there.")
    if ask("Type ROTATE to replace it: ").strip() != "ROTATE":
        say("stopped; nothing changed")
        return None
    _restrict(OUTBOX)
    token = OUTBOX / TOKEN_NAME
    if token.exists():
        raise RuntimeError(f"{token} still exists: delete it first (4)")
    r = fleet.tools.run("credentials", "rotate", "--database", str(fleet.tools.ledger), "--app-id", APP, "--output", str(token))
    if not r["ok"]:
        raise RuntimeError("the Gateway did not rotate Coder's token: " + (r["error"] or f"exit {r['exit']}"))
    say(f"  rotated; the new token is in {token}. Carry it as before.")
    return True


def delete_hub_copy(ask=input, say=print):
    token = OUTBOX / TOKEN_NAME
    if not token.exists():
        say("no hub copy of Coder's token")
        return False
    say(f"This deletes {token}. Do it only after TEST-CODER-GATEWAY passed on the Coder laptop.")
    if ask("Type DELETE to delete the hub copy: ").strip() != "DELETE":
        say("kept")
        return False
    token.unlink()
    say("hub copy deleted")
    return True


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", choices=("inspect", "setup", "rotate", "delete-hub-copy"))
    a = p.parse_args(argv)
    gm = sys.modules.get("gateway_models") or _load("gateway_models")
    if not gm.is_admin():
        print("Coder Onboarding needs administrator rights: start it from its shortcut and click Yes.")
        return 1
    action = a.action
    while True:
        if not action:
            print(f"\nCoder Onboarding {VERSION}: 1 Inspect (read-only)   2 Set up Coder's Gateway login   3 Replace its token   "
                  "4 Delete the hub copy of the token   5 Quit")
            action = {"1": "inspect", "2": "setup", "3": "rotate", "4": "delete-hub-copy"}.get(input("Choose: ").strip())
            if not action:
                return 0
        try:
            if action == "inspect":
                g, f, fl = _tools()
                doc = inspect(g, f, fl)[0]
                print("Receipt (no secrets): " + "; ".join(str(x) for x in write_receipt(doc, "inspect")))
            else:
                {"setup": setup, "rotate": rotate, "delete-hub-copy": delete_hub_copy}[action]()
        except Exception as exc:
            print(f"STOPPED: {exc}")
        if a.action:
            return 0
        action = None


if __name__ == "__main__":
    raise SystemExit(main())
