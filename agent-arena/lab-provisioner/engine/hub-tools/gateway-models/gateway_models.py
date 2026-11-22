"""Gateway Models: the administration hub's screen for which models each orchestrator may use.

    "C:\\Program Files\\Python312\\python.exe" -I gateway_models.py      (as an administrator, on the hub)

Started from the "Gateway Models" shortcut (Gateway Models.cmd asks Windows for administrator rights).
It shows every head and its models (Audit), which orchestrator may use which model, and lets the
administrator allow or remove a model for one or all orchestrators, mark a model "Do not use" on one
head, and enrol a newly found head. Apply runs the changes in the background.

How each change reaches the Inference Gateway 0.3.0 (its tools are administrator-only, so this
screen runs elevated; the Gateway has no administration over the network):
- allowed models per orchestrator: the Gateway's own bootstrap, with the clients' current policy from
  its own credentials list and only "models" changed (concurrency, queues, priorities, operations and
  context limits are carried as they are);
- a model on a head: the Gateway's own qualify (real inference on the head, 8192-token context, as
  hub > 2 does), for every allowed model on every head that has it, unless it is marked Do not use;
- Do not use: the model is taken off that head's qualified list in the ledger, the same change the
  Gateway's qualify makes when a test fails, recorded as its "qualification.invalidated" event.
  Gateway 0.3.0 has no command for it (reported in docs/handoff/GATEWAY-0.3.0-FIELD-FINDINGS.md).
  Clearing Do not use runs qualify again;
- a new head: the Gateway's own heads tool (verified inventory -> reviewed resources), then bootstrap.

It listens on 127.0.0.1 only and answers only requests from this computer that carry the token from
the address it opens, with its own Host and Origin. It stops when its page has been closed for 10
minutes, or with Close. No credential, token or prompt is read or shown.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.3.0"
UI = Path(__file__).resolve().parent / "ui"
GATEWAY_ROOT = Path(r"C:\Program Files\InferenceGateway")
GATEWAY_DATA = Path(r"C:\ProgramData\InferenceGateway")
CONSOLE_DATA = Path(r"C:\ProgramData\AgentArenaLab\gateway\models-console")
LAN = ipaddress.IPv4Network("192.0.2.0/24")
HEAD_RANGE = range(2, 200)            # heads .2-.199 (the address plan)
OLLAMA_PORT = 11434
QUALIFY_CONTEXT = 8192                # Capstone sends num_ctx 8192; the Gateway refuses more than was qualified
# Ollama names keep their case when pulled from Hugging Face, e.g. hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M.
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,150}(:[A-Za-z0-9._-]{1,60})?$")
APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
RESOURCE = re.compile(r"^head-192-0-2-(\d{1,3})$")
NOT_CHAT = re.compile(r"(?i)embed|minilm|(^|[/:-])bge|(^|[/:-])gte")
# Field results on these heads (2026-11-06): qwen3:4b reasons before answering even with think:false,
# so it never returns exactly "READY" and can never pass the Gateway's check.
KNOWN = {"qwen3:4b": "never passes the Gateway's test on these heads: this build always reasons before answering "
                     "(try qwen3:4b-instruct-2507-q4_K_M, the non-reasoning build)"}
# Projects whose models are granted one at a time and never from this screen: "Available to all" does not reach them and
# Apply cannot add a model to them (removing one stays possible). Coder starts least-privilege with one model
# (Coder Onboarding, 2026-11-12); the Fleet window's Allow adds another, explicitly.
EXPLICIT_ONLY = {"coder": "Coder's models are granted one at a time: Fleet window > Projects > Allow, or Coder Onboarding"}
# The Gateway 0.3.0 test (capacity_gateway.Gateway.qualify), copied exactly so a warm-up loads the model the way the
# test will use it: a different num_ctx makes Ollama load the model again. The test itself has a fixed 90 s deadline for
# everything (version, tags, show and the model load); a cold load on a head can take longer (field 2026-11-08: head 10,
# qwen3.5:4b 96 s, qwen3:14b over 300 s), and a test that runs out of time mid-call locks ("quarantines") the head.
QUALIFY_PROMPT = "Return exactly the word READY."
QUALIFY_OPTIONS = {"num_ctx": QUALIFY_CONTEXT, "num_predict": 24, "temperature": 0, "seed": 42}
EMBED_FIXTURE = "Synthetic qualification fixture."
WARMUP_SECONDS = 900
WARMUP_KEEP = "10m"
QUARANTINED_TEXT = "Quarantined execution must be reconciled"
BUSY_TEXT = "allocated capacity"
EXACT_READY = "the Gateway accepts only exactly READY (the model works; the Gateway's test refuses it)"
IDLE_SECONDS = 600
MAX_LIFE_SECONDS = 12 * 3600
MAX_BODY = 64 * 1024


# ---------------------------------------------------------------------------- pure helpers (tested)

def head_address(resource_id):
    m = RESOURCE.match(resource_id or "")
    return f"192.0.2.{m.group(1)}" if m else None


def resource_for(address):
    return "head-" + address.replace(".", "-")


def is_chat_model(name):
    return bool(name) and not NOT_CHAT.search(name)


def latest_evidence(observations):
    """Newest qualification result per (resource, model) from the Gateway's observation snapshot."""
    out = {}
    for q in sorted(observations or [], key=lambda q: q.get("at") or 0):
        out[(q.get("resource_id"), q.get("model"))] = q
    return out


def cell_state(*, installed, qualified, passed_last, tested, head_live, blocked):
    """One head x model square. Order matters: Do not use wins, then installation, then the Gateway's facts."""
    if blocked:
        return "blocked"
    if not installed:
        return "absent"
    if qualified:
        return "usable" if head_live else "offline"
    if tested and passed_last is False:
        return "failed"
    if tested and passed_last:
        return "lapsed"          # passed before, no longer on the Gateway's qualified list (model or Ollama changed)
    return "untested"


def build_view(snapshot, applications, scan, blocked, flags):
    """Everything the screen draws, from the Gateway's own observation and credentials list plus the live scan.

    snapshot: observe output; applications: credentials list "applications"; scan: {address: {"version", "models"}}
    (live, may be empty); blocked: set of (resource_id, model); flags: set of models marked Available to all.
    """
    resources = {r["resource_id"]: r for r in snapshot.get("resources", []) if r.get("resource_type") == "shared_inference"}
    inventory = {i["resource_id"]: i for i in snapshot.get("inventory", [])}
    evidence = latest_evidence(snapshot.get("qualification_observations"))
    heads = {}
    for rid, r in resources.items():
        address = head_address(rid)
        if not address:
            continue
        inv = inventory.get(rid) or {}
        live = scan.get(address)
        models = sorted(live["models"]) if live else sorted(m["name"] for m in inv.get("models", []))
        # Online: it answered the live scan, or the Gateway's own inventory of it is fresh. The resource's
        # "fresh" is not used: the Gateway keeps last_seen at 0 for a head with no qualified model (degraded).
        heads[rid] = {"resource_id": rid, "address": address, "in_gateway": True, "health": r.get("health"),
                      "online": bool(live) or bool(inv.get("fresh")), "reachable": bool(live) if scan else None,
                      "ollama": (live or {}).get("version") or inv.get("runtime_version"),
                      "models": models, "qualified": list(r.get("qualified_models") or []),
                      "models_source": "live scan" if live else "Gateway inventory"}
    for address, live in scan.items():
        rid = resource_for(address)
        if rid not in heads:
            heads[rid] = {"resource_id": rid, "address": address, "in_gateway": False, "health": None, "online": True,
                          "reachable": True, "ollama": live.get("version"), "models": sorted(live["models"]),
                          "qualified": [], "models_source": "live scan"}
    order = sorted(heads, key=lambda rid: int(head_address(rid).rsplit(".", 1)[1]))
    apps = [{"app_id": a["app_id"], "enabled": bool(a.get("enabled")), "models": list(a.get("models") or [])}
            for a in applications if APP_ID.match(a.get("app_id", ""))]
    names = set()
    for h in heads.values():
        names.update(h["models"])          # embedding models too; the screen hides them unless asked
    for a in apps:
        names.update(a["models"])
    names.update(flags)
    names.update(m for _, m in blocked)
    rows = []
    for name in sorted(names):
        cells = {}
        for rid in order:
            h = heads[rid]
            ev = evidence.get((rid, name))
            cells[rid] = cell_state(installed=name in h["models"], qualified=name in h["qualified"],
                                    passed_last=(bool(ev["passed"]) if ev else None), tested=ev is not None,
                                    head_live=h["in_gateway"] and h["health"] == "healthy" and h["online"],
                                    blocked=(rid, name) in blocked)
            if not h["in_gateway"] and cells[rid] not in ("absent", "blocked"):
                cells[rid] = "not-enrolled"
        rows.append({"model": name, "chat": is_chat_model(name), "note": KNOWN.get(name, ""),
                     "allowed": [a["app_id"] for a in apps if name in a["models"]],
                     "available_to_all": name in flags, "cells": cells})
    return {"heads": [heads[rid] for rid in order], "orchestrators": apps, "models": rows,
            "explicit_only": {a["app_id"]: EXPLICIT_ONLY[a["app_id"]] for a in apps if a["app_id"] in EXPLICIT_ONLY}}


def plan_changes(view, desired, *, retest=()):
    """What Apply will do, from the current view and the screen's desired state. Pure.

    desired: {"apps": {app_id: [models]}, "blocked": [[rid, model]], "flags": [models]}
    retest: (rid, model) pairs whose last test failed and that the operator asked to test again.
    Returns {"policy": {app: {"add": [], "remove": [], "models": []}}, "block": [], "unblock": [],
             "qualify": [], "skipped": [], "problems": [], "flags": []}.
    """
    problems = []
    current = {a["app_id"]: a["models"] for a in view["orchestrators"]}
    wanted = {}
    for app, models in (desired.get("apps") or {}).items():
        if app not in current:
            problems.append(f"'{app}' is not an orchestrator the Gateway knows")
            continue
        clean = []
        for m in models:
            if not isinstance(m, str) or not MODEL_NAME.match(m):
                problems.append(f"'{m}' is not a model name")
            elif m not in clean:
                clean.append(m)
        wanted[app] = clean
    notes = []
    for app in EXPLICIT_ONLY:
        if app in wanted and app in current:
            added = [m for m in wanted[app] if m not in current[app]]
            if added:
                # Never from this screen, whatever the page sent ("Available to all" ticks every orchestrator).
                wanted[app] = [m for m in wanted[app] if m in current[app]]
                notes.append(f"not given to {app}: {', '.join(added)} ({EXPLICIT_ONLY[app]})")
    policy = {}
    for app, before in current.items():
        after = wanted.get(app, before)
        if set(after) != set(before):
            if not after:
                problems.append(f"{app} would have no model left: the Gateway needs at least one per orchestrator")
            policy[app] = {"add": sorted(set(after) - set(before)), "remove": sorted(set(before) - set(after)),
                           "models": sorted(after)}
    heads = {h["resource_id"]: h for h in view["heads"]}
    now_blocked = {(rid, m) for r in view["models"] for rid, s in r["cells"].items() for m in [r["model"]] if s == "blocked"}
    want_blocked = set()
    for pair in desired.get("blocked") or []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] in heads and isinstance(pair[1], str) and MODEL_NAME.match(pair[1])):
            problems.append(f"Do not use entry {pair!r} is not a known head and a model name")
            continue
        want_blocked.add((pair[0], pair[1]))
    block = sorted(want_blocked - now_blocked)
    unblock = sorted(now_blocked - want_blocked)
    allowed = set()
    for app in current:
        allowed.update(wanted.get(app, current[app]))
    retest = {tuple(p) for p in retest}
    qualify, skipped = [], []
    state = {(rid, r["model"]): s for r in view["models"] for rid, s in r["cells"].items()}
    for model in sorted(allowed):
        for rid, h in heads.items():
            if not h["in_gateway"] or (rid, model) in want_blocked:
                continue
            s = state.get((rid, model))
            if s is None:
                s = "untested" if model in h["models"] else "absent"
            if (rid, model) in set(unblock) and model in h["models"]:
                s = "untested"
            wants = s in ("untested", "lapsed") or (s == "failed" and (rid, model) in retest)
            if wants and not h["online"]:
                # A test against an unreachable head would be recorded as a failed test.
                skipped.append({"resource_id": rid, "model": model, "reason": "the head is offline (tested when it is back and you Apply)"})
            elif wants:
                # A head the Gateway has locked ("quarantined") is tested too: run_head_tests unlocks it with the Gateway's
                # own reconcile once a warm-up shows it idle, and skips it with the reason if the Gateway refuses (busy).
                qualify.append([rid, model])
            elif s == "failed":
                skipped.append({"resource_id": rid, "model": model, "reason": "its last test failed (tick Retest to try again)"})
    flags = sorted({m for m in desired.get("flags") or [] if isinstance(m, str) and MODEL_NAME.match(m)})
    return {"policy": policy, "block": [list(p) for p in block], "unblock": [list(p) for p in unblock],
            "qualify": qualify, "skipped": skipped, "problems": problems, "flags": flags, "notes": notes}


def plan_is_empty(plan, flags_before):
    return not (plan["policy"] or plan["block"] or plan["unblock"] or plan["qualify"]) and set(plan["flags"]) == set(flags_before)


def bootstrap_policy(applications, models_by_app):
    """The Gateway bootstrap document that changes only these clients' models. Pure.

    Every other field comes from the Gateway's own credentials list, so concurrency, queue limit,
    priority, operations and context limit stay exactly as they are. No resources: heads are untouched.
    """
    clients = []
    for a in applications:
        if a["app_id"] not in models_by_app:
            continue
        c = {"app_id": a["app_id"], "credential": "provisioned", "models": list(models_by_app[a["app_id"]]),
             "max_concurrency": a["max_concurrency"], "queue_limit": a["queue_limit"], "priority": a["priority"],
             "operations": a.get("operations"), "max_context": a.get("max_context")}
        clients.append(c)
    return {"configuration_version": 1, "contract": "shared.inference/1", "clients": clients, "resources": []}


def head_inventory(address, version, models, computer):
    """agent-arena.verified-head-inventory/1 for one head the hub just reached (the Gateway's heads tool input)."""
    rid = resource_for(address)
    return {"schema": "agent-arena.verified-head-inventory/1", "generated_on": computer, "checked_from": "192.0.2.254",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": "Gateway Models console: one head to enrol",
            "heads": [{"resource_id": rid, "arena_node_id": rid, "address": address, "endpoint": f"http://{address}:{OLLAMA_PORT}",
                       "verified": {"ollama_version": version, "models_read": True,
                                    "models": [{"name": m["name"], "digest": m.get("digest", "")} for m in models],
                                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from_computer": computer}}],
            "excluded": []}


def one_line(text, limit=60):
    """A model's answer or an error on one short line: newlines as spaces, cut at limit characters. Pure."""
    t = " ".join(str(text if text is not None else "").split())
    return t if len(t) <= limit else t[:limit - 3].rstrip() + "..."


def warmup_request(model):
    """(path, body) that loads the model on the head exactly as the Gateway's test will use it. Pure."""
    if is_chat_model(model):
        return "/api/generate", {"model": model, "prompt": QUALIFY_PROMPT, "stream": False, "think": False,
                                 "keep_alive": WARMUP_KEEP, "options": dict(QUALIFY_OPTIONS)}
    return "/api/embed", {"model": model, "input": [EMBED_FIXTURE], "keep_alive": WARMUP_KEEP}


def judge_warmup(answer, done=True):
    """(ok, reason): would the Gateway's completion check accept this answer? It wants the reply, stripped, to be
    exactly READY. That is the Gateway's rule, not ours; the reason says so plainly. Pure."""
    text = str(answer if answer is not None else "")
    if done and text.strip() == "READY":
        return True, ""
    if not done:
        return False, "its answer did not finish, and the Gateway accepts only a finished answer that is exactly READY"
    if not text.strip():
        return False, "it gave an empty answer and " + EXACT_READY
    return False, f'it answered "{one_line(text)}" and ' + EXACT_READY


def judge_response(model, response):
    """(ok, reason) for a warm-up's reply: the completion check for a chat model, a non-empty vector for an embedding model."""
    response = response if isinstance(response, dict) else {}
    if is_chat_model(model):
        return judge_warmup(response.get("response"), bool(response.get("done")))
    vectors = response.get("embeddings")
    if vectors and vectors[0]:
        return True, ""
    return False, "it returned no embedding, and the Gateway's test needs one"


def warmup_error(exc, seconds=WARMUP_SECONDS):
    """The real reason a warm-up failed, in words, with Ollama's own error text for an HTTP error. Pure for a given exc."""
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            raw = exc.read(65536).decode("utf-8", "replace") if exc.fp else ""
            try:
                body = json.loads(raw).get("error") or raw
            except (ValueError, AttributeError):
                body = raw
        except Exception:
            body = ""
        return f"HTTP {exc.code} " + (one_line(body, 200) or str(exc.reason or ""))
    reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc):
        return f"it did not finish loading within {seconds // 60} minutes"
    if isinstance(exc, urllib.error.URLError):
        return f"the head did not answer over the lab cable ({one_line(reason, 120)})"
    if isinstance(exc, (ConnectionError, OSError)):
        return f"the head did not answer over the lab cable ({type(exc).__name__}: {one_line(exc, 120)})"
    return f"{type(exc).__name__}: {one_line(exc, 200)}"


GATEWAY_WORDS = (("deadline expired", "the Gateway's test ran out of time (its fixed 90 s) even though the model was loaded first"),
                 ("timed out", "the Gateway's test ran out of time (its fixed 90 s) even though the model was loaded first"),
                 ("model not installed", "the Gateway does not see the model on the head"),
                 ("Metadata does not support", "the model does not say it can do what the Gateway's test asks (Ollama's /api/show)"),
                 ("requires an observed model digest", "the head did not report the model's digest to the Gateway"))


def qualify_reason(q, predicted="", again="Commit again later"):
    """The most specific reason a Gateway test failed, never just an exit code when anything better is known. Pure."""
    error = (q or {}).get("error") or ""
    if BUSY_TEXT in error:
        return f"the head was busy with work; {again}"
    if QUARANTINED_TEXT in error:
        return "the Gateway has locked the head after an earlier timed-out test, and it could not be unlocked"
    if predicted:
        return predicted
    found = (q or {}).get("evidence_error") or ""
    if found:
        for needle, words in GATEWAY_WORDS:
            if needle.lower() in found.lower():
                return words
        return "the Gateway's test says: " + one_line(found, 200)
    if error:
        return one_line(error, 200)
    if (q or {}).get("json") and "passed" in q["json"]:
        return "its answer did not pass the Gateway's check"
    return f"exit {(q or {}).get('exit')}"


def plan_head_tests(pairs):
    """{resource: [models]} in head order, each head's models in the order given. Pure."""
    out = {}
    for rid, model in pairs:
        out.setdefault(rid, []).append(model)
    return dict(sorted(out.items(), key=lambda kv: int((head_address(kv[0]) or "0.0.0.0").rsplit(".", 1)[1])))


def summarize_head(name, outcomes):
    """'head 10: 18 usable, 5 refused by the Gateway's exact-READY rule (a, b), 1 failed to load (c)'. Pure.

    outcomes: [(model, result)] with result["kind"] one of usable, exact-ready, load, failed, not-tested."""
    groups = {}
    for model, r in outcomes:
        groups.setdefault(r["kind"], []).append(model)
    parts = [f"{len(groups.get('usable', []))} usable"]
    for kind, words in (("exact-ready", "refused by the Gateway's exact-READY rule"), ("load", "failed to load"),
                        ("failed", "failed the Gateway's test"), ("not-tested", "not tested")):
        if groups.get(kind):
            parts.append(f"{len(groups[kind])} {words} ({', '.join(groups[kind])})")
    return f"{name}: " + ", ".join(parts)


def head_name(rid):
    address = head_address(rid)
    return "head " + address.rsplit(".", 1)[1] if address else rid


def run_head_tests(tools, pairs, *, http, say, log=None, again="Commit again later", parallel=True):
    """The Gateway's test for each (resource, model): the heads side by side, the models on one head one after another
    (the Gateway takes a head for itself during its test, so heads can test at the same time but one head cannot).

    For each pair: load the model on the head first, directly on its Ollama, with the test's own request (a cold load
    can take longer than the test's fixed 90 s, and a test that runs out of time locks the head); predict the test's
    answer check; unlock the head with the Gateway's own reconcile if an earlier timed-out test locked it (only now,
    after the warm-up returned, so the head is idle); then the Gateway's qualify. A model that does not load is not
    given to the Gateway's test (it would only run out of time and lock the head).

    Returns {(resource, model): {"passed", "why", "kind", "load_seconds"}}; say(text) gets self-contained lines.
    """
    lock = threading.Lock()
    results = {}

    def tell(text):
        with lock:
            say(text)

    def note(rid, model, r):
        with lock:
            results[(rid, model)] = r
            if log:
                try:
                    log(rid, model, r)
                except Exception:
                    pass

    def head_state(rid):
        """(health, busy) from the Gateway's own observation; (None, False) if it cannot be read."""
        try:
            snap = tools.snapshot()
        except Exception:
            return None, False
        health = next((r.get("health") for r in snap.get("resources", []) if r.get("resource_id") == rid), None)
        busy = any(x.get("resource_id") == rid for x in snap.get("leases") or [])
        return health, busy

    def unlock(rid):
        """None when unlocked, else why not."""
        r = tools.reconcile(rid)
        if r.get("ok"):
            tell(f"  {head_name(rid)} was locked by an earlier timed-out test; unlocked after checking it is idle")
            return None
        err = r.get("error") or f"exit {r.get('exit')}"
        if "active reservation" in err or BUSY_TEXT in err:
            return f"the head is busy with work; {again}"
        return f"the Gateway would not unlock the head ({one_line(err, 160)})"

    def one(rid, model):
        name = head_name(rid)
        address = head_address(rid)
        health, busy = head_state(rid)
        if busy:
            return {"passed": False, "why": f"the head is busy with work; {again}", "kind": "not-tested", "load_seconds": None}, True
        path, body = warmup_request(model)
        began = time.monotonic()
        try:
            response = http(address, path, body, timeout=WARMUP_SECONDS)
        except Exception as exc:
            return {"passed": False, "why": "it could not be loaded on the head: " + warmup_error(exc), "kind": "load",
                    "load_seconds": None}, False
        took = time.monotonic() - began
        tell(f"  {model} on {name}: loading on the head (took {took:.0f} s)")
        _, predicted = judge_response(model, response)
        if health == "quarantined":
            refused = unlock(rid)
            if refused:
                return {"passed": False, "why": refused, "kind": "not-tested", "load_seconds": took}, True
        q = tools.qualify(rid, model)
        if QUARANTINED_TEXT in (q.get("error") or ""):
            refused = unlock(rid)
            if refused:
                return {"passed": False, "why": refused, "kind": "not-tested", "load_seconds": took}, True
            q = tools.qualify(rid, model)
        passed = bool(q.get("json") and q["json"].get("passed"))
        if passed:
            return {"passed": True, "why": "", "kind": "usable", "load_seconds": took}, False
        why = qualify_reason(q, predicted, again)
        if BUSY_TEXT in (q.get("error") or ""):
            return {"passed": False, "why": why, "kind": "not-tested", "load_seconds": took}, True
        return {"passed": False, "why": why, "kind": "exact-ready" if predicted else "failed", "load_seconds": took}, False

    def head(rid, models):
        name = head_name(rid)
        stop = None
        for model in models:
            if stop:
                r = {"passed": False, "why": stop, "kind": "not-tested", "load_seconds": None}
                tell(f"  {model} on {name}: not tested ({stop})")
                note(rid, model, r)
                continue
            try:
                r, halt = one(rid, model)
            except Exception as exc:                    # one model's trouble does not stop the head or the others
                r, halt = {"passed": False, "why": f"{type(exc).__name__}: {one_line(exc, 200)}", "kind": "failed", "load_seconds": None}, False
            if r["passed"]:
                tell(f"  {model} on {name}: passed, usable now")
            elif r["kind"] == "not-tested":
                tell(f"  {model} on {name}: not tested ({r['why']})")
            else:
                tell(f"  {model} on {name}: FAILED: {r['why']}")
            note(rid, model, r)
            if halt:
                stop = r["why"]
                tell(f"  {name}: its remaining tests are skipped ({stop})")

    plan = plan_head_tests(pairs)
    if parallel and len(plan) > 1:
        threads = [threading.Thread(target=head, args=(rid, models), name=f"test-{rid}", daemon=True) for rid, models in plan.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for rid, models in plan.items():
            head(rid, models)
    for rid, models in plan.items():
        for m in models:                                # a lost thread must not lose its pairs
            results.setdefault((rid, m), {"passed": False, "why": "the test stopped unexpectedly", "kind": "failed", "load_seconds": None})
        tell(summarize_head(head_name(rid), [(m, results[(rid, m)]) for m in models]))
    return results


def install_note(model, heads):
    """The text the operator follows at each head (put the head on Wi-Fi, pull, put it back)."""
    names = ", ".join(heads)
    return "\r\n".join([
        f"INSTALL {model} ON {names}",
        "=" * 60,
        "At each head:",
        "1. Unplug the lab cable and connect the head to Wi-Fi (internet).",
        "2. Open PowerShell (Start, type PowerShell, Enter) and paste this line, then Enter:",
        "",
        f"       ollama pull {model}",
        "",
        "   Wait until it says success. It does not need administrator rights.",
        "3. Disconnect Wi-Fi and plug the lab cable back in.",
        "",
        "Back on the hub, in Gateway Models: click Check installs. The heads that now have it",
        "show it; if the model is allowed for an orchestrator, Apply tests it on those heads.",
        ""])


# ---------------------------------------------------------------------------- the Gateway, through its own tools

class GatewayTools:
    def __init__(self, python, release, ledger, evidence_dir, operator):
        self.python, self.release, self.ledger = str(python), Path(release), Path(ledger)
        self.launcher = self.release / "inference_gateway" / "service_launcher.py"
        self.evidence_dir, self.operator = Path(evidence_dir), operator

    def run(self, *args, timeout=180):
        p = subprocess.run([self.python, "-I", str(self.launcher), *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = p.stdout.strip()
        data = None
        if out:
            try:
                data = json.loads(out)
            except ValueError:
                i, j = out.find("{"), out.rfind("}")
                if i >= 0 and j > i:
                    try:
                        data = json.loads(out[i:j + 1])
                    except ValueError:
                        data = None
        ok = p.returncode == 0 and isinstance(data, dict) and data.get("ok", True) is True
        err = (data or {}).get("error") if isinstance(data, dict) else None
        return {"ok": ok, "exit": p.returncode, "json": data, "error": err or (p.stderr.strip().splitlines() or [""])[-1][:300]}

    def snapshot(self):
        r = self.run("observe", "--database", str(self.ledger), "--limit", "1000", timeout=60)
        if not r["ok"]:
            raise RuntimeError("the Gateway's observe tool failed: " + (r["error"] or f"exit {r['exit']}"))
        return r["json"]

    def applications(self):
        r = self.run("credentials", "list", "--database", str(self.ledger), timeout=60)
        if not r["ok"]:
            raise RuntimeError("the Gateway's credentials list failed: " + (r["error"] or f"exit {r['exit']}"))
        return r["json"]["applications"]

    def bootstrap(self, policy_file):
        return self.run("bootstrap", "--database", str(self.ledger), "--policy", str(policy_file), timeout=120)

    def heads(self, inventory_file, base_policy_file, applications, output):
        return self.run("heads", "--inventory", str(inventory_file), "--policy", str(base_policy_file),
                        "--applications", ",".join(applications), "--output", str(output), timeout=60)

    def qualify(self, resource_id, model):
        # An embedding model is tested by the Gateway with a real /api/embed (purpose "embedding"); every other model
        # with a chat answer (purpose "completion"). Capstone 6.0.0-rc.4's retrieval needs an embedding model.
        purpose = "completion" if is_chat_model(model) else "embedding"
        out = self.evidence_dir / "qual-{}-{}-{}.json".format(resource_id, re.sub(r"[^\w.-]", "_", model), time.strftime("%Y%m%d-%H%M%S"))
        r = self.run("qualify", "--database", str(self.ledger), "--resource", resource_id, "--model", model, "--purpose", purpose,
                     "--operator", self.operator, "--max-context", str(QUALIFY_CONTEXT), "--output", str(out), timeout=200)
        # The qualify tool prints only passed/evidence_id; its reason for a failure is in the evidence file it wrote.
        try:
            evidence = json.loads(out.read_text(encoding="utf-8"))
            r["evidence_error"] = evidence.get("error") if isinstance(evidence, dict) else None
        except (OSError, ValueError):
            r["evidence_error"] = None
        return r

    def reconcile(self, resource_id):
        """The Gateway's own unlock of a quarantined head (quarantined -> unqualified, tested again before use). Call it
        only when the head is known to be idle; the Gateway refuses while the head has an active reservation."""
        return self.run("lifecycle", "reconcile-resource", "--database", str(self.ledger), "--resource", resource_id,
                        "--operator", self.operator, "--confirmed-idle", timeout=60)

    def withdraw(self, resource_id, model):
        """Take one model off one head's qualified list (Do not use). Same columns and event the Gateway's
        qualify uses on a failed test; compare-and-set, so a health refresh in between is never overwritten."""
        db = sqlite3.connect(str(self.ledger), timeout=30, isolation_level=None)
        try:
            cols = {r[1] for r in db.execute("PRAGMA table_info(capacity_resources)")}
            ev = {r[1] for r in db.execute("PRAGMA table_info(capacity_events)")}
            if not {"resource_id", "qualified_models"} <= cols or not {"app_id", "request_id", "event_type", "payload", "at"} <= ev:
                raise RuntimeError("the ledger is not the Gateway 0.3.0 layout this console knows; nothing changed")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT qualified_models FROM capacity_resources WHERE resource_id=?", (resource_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                raise RuntimeError(f"{resource_id} is not in the Gateway")
            models = json.loads(row[0])
            if model not in models:
                db.execute("COMMIT")
                return False
            changed = db.execute("UPDATE capacity_resources SET qualified_models=? WHERE resource_id=? AND qualified_models=?",
                                 (json.dumps([m for m in models if m != model]), resource_id, row[0])).rowcount
            if not changed:
                db.execute("ROLLBACK")
                raise RuntimeError("the head changed while marking it; try again")
            db.execute("INSERT INTO capacity_events(app_id,request_id,event_type,payload,at) VALUES(?,?,?,?,?)",
                       ("infrastructure", None, "qualification.invalidated",
                        json.dumps({"resource_id": resource_id, "model": model, "operator": self.operator, "reason": "operator_do_not_use"}), time.time()))
            db.execute("COMMIT")
            return True
        finally:
            db.close()


def ollama_request(address, path, body=None, timeout=30, method=None):
    """One call to a head's Ollama over the lab cable (no proxy). Raises urllib's errors; warmup_error() reads them."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{address}:{OLLAMA_PORT}{path}", data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        raw = r.read(8 << 20)
    return json.loads(raw) if raw.strip() else {}


def scan_heads(addresses=None, timeout=1.5):
    """Live: every head address that answers as Ollama from this computer, with its models (name, digest)."""
    addresses = addresses or [f"192.0.2.{n}" for n in HEAD_RANGE]

    def one(address):
        try:
            with urllib.request.urlopen(f"http://{address}:{OLLAMA_PORT}/api/version", timeout=timeout) as r:
                version = json.loads(r.read(65536)).get("version")
            with urllib.request.urlopen(f"http://{address}:{OLLAMA_PORT}/api/tags", timeout=5) as r:
                tags = json.loads(r.read(4 << 20)).get("models", [])
            return address, {"version": version, "models": {m["name"]: m.get("digest", "") for m in tags if m.get("name")}}
        except Exception:
            return address, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        found = {a: v for a, v in pool.map(one, addresses) if v}
    return found


# ---------------------------------------------------------------------------- console state

class Console:
    def __init__(self, tools, data_dir, *, scanner=scan_heads, computer=None, http=ollama_request):
        self.tools, self.data = tools, Path(data_dir)
        self.scanner, self.http = scanner, http
        self.computer = computer or os.environ.get("COMPUTERNAME", "hub")
        self.lock = threading.Lock()
        self.scan = {}
        self.scan_at = None
        self.job = None
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "evidence").mkdir(exist_ok=True)
        (self.data / "install-notes").mkdir(exist_ok=True)

    # state files (administrators only: the installer restricts this folder)
    def _read(self, name, default):
        p = self.data / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name, value):
        p = self.data / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def blocked(self):
        return {(b["resource_id"], b["model"]) for b in self._read("do-not-use.json", {"entries": []}).get("entries", [])}

    def flags(self):
        return set(self._read("available-to-all.json", {"models": []}).get("models", []))

    def installs(self):
        return self._read("install-requests.json", {"requests": []}).get("requests", [])

    def log(self, entry):
        with open(self.data / "changes.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "operator": self.tools.operator, **entry}) + "\n")

    def view(self):
        snap = self.tools.snapshot()
        apps = self.tools.applications()
        v = build_view(snap, apps, self.scan, self.blocked(), self.flags())
        v["gateway"] = {"ledger_id": snap.get("ledger_id"), "admission_open": snap.get("admission_open"),
                        "observed_at": snap.get("observed_at"), "release": self.tools.release.name}
        v["scan_at"] = self.scan_at
        v["installs"] = self._install_status(v)
        v["job"] = self.job_status()
        v["known"] = KNOWN
        v["computer"] = self.computer
        return v, apps

    def audit(self):
        found = self.scanner()
        with self.lock:
            self.scan, self.scan_at = found, time.time()
        return found

    def _install_status(self, view):
        heads = {h["address"]: h for h in view["heads"]}
        out = []
        for r in self.installs():
            status = {a: ("installed" if a in heads and r["model"] in heads[a]["models"] else "waiting") for a in r["heads"]}
            out.append({**r, "status": status, "done": all(s == "installed" for s in status.values())})
        return out

    def add_install(self, model, addresses):
        if not MODEL_NAME.match(model or ""):
            raise ValueError("not a model name (for example llama3.2:3b)")
        bad = [a for a in addresses if a not in [f"192.0.2.{n}" for n in HEAD_RANGE]]
        if bad or not addresses:
            raise ValueError("choose one or more head addresses 192.0.2.2-199")
        reqs = [r for r in self.installs() if r["model"] != model]
        reqs.append({"model": model, "heads": sorted(set(addresses), key=lambda a: int(a.rsplit(".", 1)[1])), "asked_at": time.strftime("%Y-%m-%d %H:%M")})
        self._write("install-requests.json", {"requests": reqs})
        note = install_note(model, [a.replace("192.0.2", "") for a in reqs[-1]["heads"]])
        path = self.data / "install-notes" / ("install-" + re.sub(r"[^\w.-]", "_", model) + ".txt")
        path.write_text(note, encoding="utf-8")
        self.log({"action": "install request", "model": model, "heads": reqs[-1]["heads"]})
        return {"note": note, "file": str(path)}

    def clear_installs(self, done_only=True):
        view, _ = self.view()
        keep = [r for r in view["installs"] if done_only and not r["done"]]
        self._write("install-requests.json", {"requests": [{k: r[k] for k in ("model", "heads", "asked_at")} for r in keep]})

    # jobs: one at a time, in the background, with progress lines for the screen
    def job_status(self):
        j = self.job
        if not j:
            return None
        return {"id": j["id"], "title": j["title"], "running": j["running"], "lines": list(j["lines"]), "result": j.get("result")}

    def start_job(self, title, work):
        with self.lock:
            if self.job and self.job["running"]:
                raise RuntimeError("another change is still running; wait for it to finish")
            self.job = {"id": secrets.token_hex(4), "title": title, "running": True, "lines": []}
            job = self.job

        def say(text):
            job["lines"].append(time.strftime("%H:%M:%S ") + text)

        def body():
            try:
                job["result"] = work(say)
            except Exception as exc:  # the screen shows it; nothing is retried behind the operator's back
                say("STOPPED: " + str(exc))
                job["result"] = {"ok": False, "error": str(exc)}
            finally:
                job["running"] = False
        threading.Thread(target=body, daemon=True).start()
        return job["id"]

    def apply(self, desired, retest):
        view, apps = self.view()
        plan = plan_changes(view, desired, retest=retest)
        if plan["problems"]:
            raise ValueError("; ".join(plan["problems"]))

        def work(say):
            failures = []
            if plan["policy"]:
                models = {a["app_id"]: a["models"] for a in apps}
                for app, change in plan["policy"].items():
                    models[app] = change["models"]
                    say(f"{app}: allow {', '.join(change['models'])}" + (f"  (+ {', '.join(change['add'])})" if change["add"] else "") + (f"  (- {', '.join(change['remove'])})" if change["remove"] else ""))
                doc = bootstrap_policy(apps, {app: models[app] for app in plan["policy"]})
                pf = self.data / "evidence" / ("policy-{}.json".format(time.strftime("%Y%m%d-%H%M%S")))
                pf.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                r = self.tools.bootstrap(pf)
                if not r["ok"]:
                    raise RuntimeError("the Gateway refused the model change: " + (r["error"] or f"exit {r['exit']}") + " (nothing else was done)")
                say("the Gateway accepted the allowed-model change")
                self._write_approved(models, apps)
                self.log({"action": "allowed models", "changes": plan["policy"]})
            for rid, model in plan["block"]:
                entries = self._read("do-not-use.json", {"entries": []})["entries"]
                if not any(e["resource_id"] == rid and e["model"] == model for e in entries):
                    entries.append({"resource_id": rid, "model": model, "since": time.strftime("%Y-%m-%d %H:%M"), "by": self.tools.operator})
                    self._write("do-not-use.json", {"entries": entries})
                changed = self.tools.withdraw(rid, model)
                say(f"{model} on {head_address(rid)}: Do not use" + ("" if changed else " (it was not in use there)"))
                self.log({"action": "do not use", "resource_id": rid, "model": model, "withdrawn": changed})
            for rid, model in plan["unblock"]:
                entries = [e for e in self._read("do-not-use.json", {"entries": []})["entries"] if not (e["resource_id"] == rid and e["model"] == model)]
                self._write("do-not-use.json", {"entries": entries})
                say(f"{model} on {head_address(rid)}: Do not use cleared")
                self.log({"action": "do not use cleared", "resource_id": rid, "model": model})
            self._write("available-to-all.json", {"models": plan["flags"]})
            pairs = [(rid, model) for rid, model in plan["qualify"]]
            if pairs:
                say(f"testing {len(pairs)} head/model pair(s) on {len(plan_head_tests(pairs))} head(s), the heads side by side: each model "
                    "is first loaded on its head (a large model can take several minutes), then the Gateway tests it")
                results = self.test_pairs(pairs, say, again="Apply again in a minute")
                failures.extend(f"{model} on {head_address(rid)}" for rid, model in pairs if not results[(rid, model)]["passed"])
            for s in plan["skipped"]:
                say(f"not tested: {s['model']} on {head_address(s['resource_id'])} - {s['reason']}")
            say("done" + (f"; {len(failures)} test(s) failed: {', '.join(failures)}" if failures else ""))
            return {"ok": not failures, "failures": failures}
        return self.start_job("Apply changes", work), plan

    def test_pairs(self, pairs, say, *, again, action="qualify"):
        """The Gateway's tests with a warm-up on each head first (run_head_tests), logged to changes.log."""
        def log(rid, model, r):
            self.log({"action": action, "resource_id": rid, "model": model, "passed": r["passed"], "error": None if r["passed"] else r["why"],
                      "load_seconds": None if r["load_seconds"] is None else round(r["load_seconds"], 1)})
        return run_head_tests(self.tools, pairs, http=self.http, say=say, log=log, again=again)

    def _write_approved(self, models, apps):
        """The hub's copy of the approved policy, which hub > 2 uses from now on (so a USB re-run never undoes this)."""
        clients = [{"app_id": a["app_id"], "credential": "provisioned", "models": list(models[a["app_id"]]),
                    "max_concurrency": a["max_concurrency"], "queue_limit": a["queue_limit"], "priority": a["priority"],
                    **({"operations": a["operations"]} if a.get("operations") else {}),
                    **({"max_context": a["max_context"]} if a.get("max_context") else {})} for a in apps]
        self._write("gateway-policy-approved.json", {"configuration_version": 1, "contract": "shared.inference/1",
                                                     "written_by": "Gateway Models console " + VERSION, "clients": clients})

    def enrol(self, address):
        if address not in self.scan:
            raise ValueError(f"{address} did not answer in the last Audit; click Audit first")
        view, apps = self.view()
        if any(h["address"] == address and h["in_gateway"] for h in view["heads"]):
            raise ValueError(f"{address} is already in the Gateway")
        live = self.scan[address]
        app_ids = [a["app_id"] for a in apps if a.get("enabled")]

        def work(say):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            inv = self.data / "evidence" / f"inventory-{address}-{stamp}.json"
            inv.write_text(json.dumps(head_inventory(address, live["version"], [{"name": n, "digest": d} for n, d in live["models"].items()], self.computer), indent=2), encoding="utf-8")
            base = self.data / "evidence" / f"base-{stamp}.json"
            base.write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [], "resources": []}), encoding="utf-8")
            review = self.data / "evidence" / f"head-review-{address}-{stamp}.json"
            say(f"{address}: the Gateway's heads tool checks it (for {', '.join(app_ids)})")
            r = self.tools.heads(inv, base, app_ids, review)
            if not r["ok"]:
                refused = (r["json"] or {}).get("refused") if isinstance(r["json"], dict) else None
                raise RuntimeError("the Gateway's heads tool refused it: " + (json.dumps(refused) if refused else (r["error"] or f"exit {r['exit']}")))
            r = self.tools.bootstrap(review)
            if not r["ok"]:
                raise RuntimeError("bootstrap refused the head: " + (r["error"] or f"exit {r['exit']}"))
            say(f"{address} is in the Gateway now, for {', '.join(app_ids)}")
            self.log({"action": "head enrolled", "address": address, "applications": app_ids})
            allowed = sorted({m for a in apps for m in a["models"]} & set(live["models"]) - {m for rid, m in self.blocked() if rid == resource_for(address)})
            if allowed:
                say(f"testing {len(allowed)} model(s) on {address}, one after another: each is first loaded on the head "
                    "(a large model can take several minutes), then the Gateway tests it")
                self.test_pairs([(resource_for(address), model) for model in allowed], say, again="test it again later with Apply")
            else:
                say(f"none of the allowed models is installed on {address} yet")
            say("done")
            return {"ok": True}
        return self.start_job(f"Add {address} to the Gateway", work)


# ---------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    console: Console = None
    token = ""
    port = 0
    last_seen = [time.time()]
    stop = None
    server_version = "GatewayModels/" + VERSION

    def log_message(self, *args):
        pass

    def _own(self):
        own = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("Host") not in own:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in own}

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return hmac.compare_digest(self.headers.get("X-Console-Token", ""), self.token)

    def do_GET(self):
        if not self._own():
            return self._send(403, {"error": "this screen answers only its own page on this computer"})
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            f = (UI / name).resolve()
            if UI.resolve() not in f.parents or not f.is_file() or f.suffix not in (".html", ".css", ".js", ".svg", ".png"):
                return self._send(404, {"error": "not found"})
            return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] + ("; charset=utf-8" if f.suffix in (".html", ".css", ".js") else ""))
        if not self._authorized():
            return self._send(401, {"error": "open this screen from its shortcut"})
        Handler.last_seen[0] = time.time()
        try:
            if path == "/api/state":
                v, _ = self.console.view()
                return self._send(200, v)
            if path == "/api/job":
                return self._send(200, {"job": self.console.job_status()})
            if path == "/api/ping":
                return self._send(200, {"ok": True})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._own():
            return self._send(403, {"error": "this screen answers only its own page on this computer"})
        if not self._authorized():
            return self._send(401, {"error": "open this screen from its shortcut"})
        Handler.last_seen[0] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "not JSON"})
        path = self.path.split("?", 1)[0]
        c = self.console
        try:
            if path == "/api/audit":
                found = c.audit()
                v, _ = c.view()
                return self._send(200, {"found": len(found), "view": v})
            if path == "/api/plan":
                v, _ = c.view()
                plan = plan_changes(v, body.get("desired") or {}, retest=[tuple(p) for p in body.get("retest") or []])
                plan["empty"] = plan_is_empty(plan, c.flags())
                return self._send(200, plan)
            if path == "/api/apply":
                job, plan = c.apply(body.get("desired") or {}, [tuple(p) for p in body.get("retest") or []])
                return self._send(200, {"job": job, "plan": plan})
            if path == "/api/enrol":
                return self._send(200, {"job": c.enrol(str(body.get("address", "")))})
            if path == "/api/install":
                return self._send(200, c.add_install(str(body.get("model", "")).strip(), [str(a) for a in body.get("heads") or []]))
            if path == "/api/install/clear":
                c.clear_installs()
                v, _ = c.view()
                return self._send(200, v)
            if path == "/api/close":
                self._send(200, {"closing": True})
                threading.Thread(target=self.stop, daemon=True).start()
                return None
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_release(gateway_root, gateway_data):
    state = Path(gateway_data) / "deployment" / "deployment-state.json"
    try:
        current = json.loads(state.read_text(encoding="utf-8")).get("current")
    except (OSError, ValueError):
        current = None
    if current:
        r = Path(gateway_root) / "releases" / current
        if (r / "inference_gateway" / "service_launcher.py").is_file():
            return r
    found = sorted((p for p in (Path(gateway_root) / "releases").glob("*") if (p / "inference_gateway" / "service_launcher.py").is_file()), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit("No Inference Gateway release is installed here (Administration hub > 2 installs it).")
    return found[-1]


def find_ledger(gateway_data):
    cfg = Path(gateway_data) / "config" / "gateway-service.json"
    try:
        ledger = json.loads(cfg.read_text(encoding="utf-8")).get("ledger")
        if ledger:
            return Path(ledger)
    except (OSError, ValueError):
        pass
    return Path(gateway_data) / "state" / "capacity-ledger.db"


def build_server(console, port=0, token=None):
    token = token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    Handler.console, Handler.token, Handler.port = console, token, server.server_address[1]
    Handler.stop = server.shutdown
    return server, token


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway-root", default=str(GATEWAY_ROOT))
    p.add_argument("--gateway-data", default=str(GATEWAY_DATA))
    p.add_argument("--release", help="the Gateway release folder (default: the installed current release)")
    p.add_argument("--ledger", help="(rehearsal) a ledger other than the installed Gateway's")
    p.add_argument("--data", default=str(CONSOLE_DATA), help="this console's own folder (administrators only)")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--token", help="(rehearsal) a fixed token")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--rehearsal", action="store_true", help="allow running without administrator rights against --ledger")
    p.add_argument("--self-check", action="store_true", help="read the Gateway once, print a JSON summary and stop (used by the installer)")
    a = p.parse_args(argv)
    if not a.rehearsal and not is_admin():
        print("Gateway Models needs administrator rights (the Gateway's ledger is administrator-only).")
        print("Start it from its shortcut and click Yes.")
        return 1
    release = Path(a.release) if a.release else find_release(a.gateway_root, a.gateway_data)
    ledger = Path(a.ledger) if a.ledger else find_ledger(a.gateway_data)
    if not ledger.is_file():
        print(f"The Gateway's ledger is not at {ledger} (Administration hub > 2 installs the Gateway).")
        return 1
    tools = GatewayTools(sys.executable, release, ledger, Path(a.data) / "evidence", os.environ.get("USERNAME", "operator"))
    console = Console(tools, a.data)
    if a.self_check:
        # Read-only: the Gateway's observe and credentials list once, and the screen's own files.
        try:
            v, _ = console.view()
            missing = [n for n in ("index.html", "app.js", "app.css") if not (UI / n).is_file()]
            if missing:
                raise RuntimeError("screen files missing: " + ", ".join(missing))
            print(json.dumps({"ok": True, "version": VERSION, "release": release.name, "ledger_id": v["gateway"]["ledger_id"],
                              "orchestrators": {o["app_id"]: o["models"] for o in v["orchestrators"]},
                              "heads": [h["address"] for h in v["heads"]], "models": len(v["models"])}))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
    server, token = build_server(console, a.port, a.token)
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={token}"

    started = time.time()

    def idle():
        # The page pings every 60 s while it is open (also with a dialog open). Closed for 10 minutes,
        # or open for 12 hours, the screen stops; never in the middle of a change.
        while True:
            time.sleep(30)
            gone = time.time() - Handler.last_seen[0] > IDLE_SECONDS or time.time() - started > MAX_LIFE_SECONDS
            if gone and not (console.job and console.job["running"]):
                server.shutdown()
                return
    threading.Thread(target=idle, daemon=True).start()
    print(f"Gateway Models {VERSION}: Gateway release {release.name}, ledger {ledger}")
    print("The screen is open in your browser. Keep this window open while you use it;")
    print("it closes by itself 10 minutes after the page is closed.")
    print(url if a.rehearsal else "(address with its one-time token opened in the browser)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
