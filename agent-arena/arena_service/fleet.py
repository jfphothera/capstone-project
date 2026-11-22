"""The admin hub's Fleet window: every head, its tier and models, the catalog, the projects, and changes
collected for one Commit.

Read side (as Arena's own standard account): Arena's registry and the heads' hardware reports, a scan of
the head addresses over the lab cable (each head's Ollama answers the hub), the Gateway's published
observation, the operator's catalog, and fleet-state.json, which the elevated Fleet Commit helper publishes
(models per project, projects per head, Do not use). Arena never reads the Gateway's ledger.

Write side: none of its own. Commit writes the collected changes to the fleet requests folder and asks
Windows to start the Fleet Commit helper as an administrator (one password prompt, an administrator's);
the helper shows the list, asks for APPROVE in its window, makes the changes through the Gateway's own
tools and the hub's model library, and reports progress in the fleet status folder, shown here.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path

FLEET_DIR = r"C:\ProgramData\AgentArenaLab\fleet"
HELPER = r"C:\Program Files\AgentArenaLab\GatewayModels\fleet_commit.py"
PYTHON = r"C:\Program Files\Python312\python.exe"
REQUEST_SCHEMA = "agent-arena.fleet-request/1"
STATE_SCHEMA = "agent-arena.fleet-state/1"
HEADS = [f"192.0.2.{n}" for n in range(2, 200)]
OLLAMA_PORT = 11434
SCAN_EVERY = 60
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,150}(:[A-Za-z0-9._-]{1,60})?$")
APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
OPS = ("allow", "disallow", "head-projects", "do-not-use", "install", "uninstall", "test-missing")
# The projects the lab plans for; coder shows before it has a Gateway login (open item O5).
KNOWN_PROJECTS = {"capstone": "Capstone (Ticketing)", "capstone-research": "Capstone research", "coder": "Coder"}
NOT_CHAT = re.compile(r"(?i)embed|minilm|(^|[/:-])bge|(^|[/:-])gte")   # as gateway_models.NOT_CHAT
GB = 1024 ** 3


def head_address(value):
    try:
        ip = ipaddress.IPv4Address(str(value))
    except ValueError:
        return None
    s = str(ip)
    return s if s in HEADS else None


def tier_for(telemetry, tiers, override=None, inventory=None):
    """The head's hardware tier from its own report (RAM, NVIDIA VRAM), else from the lab's head list (the
    provisioner's head_inventory: heads without an Arena viewer, such as heads 2 and 10, have no report), by the
    catalog's tier rules, or the operator's choice. Pure."""
    ram = vram = None
    gpu = ""
    found = "from its report"
    if telemetry:
        if telemetry.get("ram_total_bytes"):
            ram = round(telemetry["ram_total_bytes"] / GB, 1)
        gpus = [g for g in telemetry.get("gpus") or [] if isinstance(g, dict) and g.get("vram_total_bytes")]
        if gpus:
            best = max(gpus, key=lambda g: g["vram_total_bytes"])
            vram, gpu = round(best["vram_total_bytes"] / GB, 1), str(best.get("name") or "")
    # Each fact the report lacks comes from the lab's head list: a head with no viewer has no report at all, and head
    # 2's viewer reports its RAM but not its NVIDIA GPU, which made the GPU workstation a "CPU, 32 GB" head.
    if inventory:
        used = []
        if ram is None and inventory.get("ram_gb") is not None:
            ram = inventory["ram_gb"]
            used.append("RAM")
        if vram is None and inventory.get("vram_gb") is not None:
            vram, gpu = inventory["vram_gb"], str(inventory.get("gpu") or "")
            used.append("GPU")
        if used:
            found = "from the lab's head list" if not telemetry else f"from its report; {' and '.join(used)} from the lab's head list"
    facts = {"ram_gb": ram, "vram_gb": vram, "gpu": gpu}
    if override and override in tiers:
        return {"id": override, "label": tiers[override]["label"], "source": "chosen by the operator", **facts}
    for tid, t in tiers.items():
        m = t.get("match") or {}
        if m.get("kind") == "gpu" and vram is not None and m["vram_gb"][0] <= vram < m["vram_gb"][1] and not tid.endswith("-OPT"):
            return {"id": tid, "label": t["label"], "source": found, **facts}
    for tid, t in tiers.items():
        m = t.get("match") or {}
        if m.get("kind") == "cpu" and vram is None and ram is not None and m["ram_gb"][0] <= ram < m["ram_gb"][1]:
            return {"id": tid, "label": t["label"], "source": found, **facts}
    return {"id": None, "label": "unknown (no hardware report and not in the lab's head list): choose its Tier below", "source": "unknown", **facts}


def check_change(c):
    """One pending change, in the helper's request format, or ValueError. Pure. The helper checks again."""
    if not isinstance(c, dict) or c.get("op") not in OPS:
        raise ValueError("not a fleet change")
    out = {"op": c["op"]}
    if c["op"] == "test-missing":
        return out
    if c["op"] in ("allow", "disallow"):
        if not APP_ID.match(str(c.get("app", ""))):
            raise ValueError("choose a project")
        out["app"] = c["app"]
    else:
        a = head_address(c.get("address"))
        if not a:
            raise ValueError(f"{c.get('address')!r} is not a head address (192.0.2.2-199)")
        out["address"] = a
    if c["op"] == "head-projects":
        apps = c.get("applications")
        if not isinstance(apps, list) or not apps or not all(APP_ID.match(str(x)) for x in apps):
            raise ValueError("a head serves one or more projects")
        out["applications"] = sorted(set(apps))
    else:
        if not MODEL_NAME.match(str(c.get("model", ""))):
            raise ValueError(f"{c.get('model')!r} is not a model name")
        out["model"] = c["model"]
    if c["op"] == "do-not-use":
        out["on"] = bool(c.get("on"))
    return out


def describe(c):
    """The same words as the Fleet Commit helper's window and results (lab-provisioner tests keep them equal)."""
    h = f"head {c['address'].rsplit('.', 1)[1]}" if c.get("address") else ""
    return {"allow": f"Allow {c.get('model')} for {c.get('app')}", "disallow": f"Stop allowing {c.get('model')} for {c.get('app')}",
            "head-projects": f"{h} ({c.get('address')}) serves: {', '.join(c.get('applications') or [])}",
            "do-not-use": f"Do not use {c.get('model')} on {h}" if c.get("on") else f"Use {c.get('model')} on {h} again",
            "install": f"Install {c.get('model')} on {h} (an exact copy from the hub's library)",
            "uninstall": f"Uninstall {c.get('model')} from {h} (out of the Gateway first)",
            "test-missing": "Test every allowed model that is not usable yet, on each Gateway head that has it"}.get(c["op"], c["op"])


def still_to_do(changes, status):
    """After a Commit: the changes that did not complete (to try again); none when it was not run. Pure."""
    if not status or status.get("state") not in ("done", "failed"):
        return list(changes)
    done = {r["change"] for r in status.get("results") or [] if r.get("ok")}
    return [c for c in changes if describe(c) not in done]


def change_key(c):
    return json.dumps({k: v for k, v in c.items() if k not in ("on", "applications")}, sort_keys=True)


def merge_scan(previous, found, now, keep=180):
    """The new scan, plus heads that answered within `keep` seconds but not this time (one slow answer must not
    make a head vanish: head 7 on DESKTOP-REDACT10, 2026-11-07). Pure. Entries carry seen_at; a kept one is marked late."""
    out = {a: dict(v, seen_at=now, late=False) for a, v in found.items()}
    for a, v in (previous or {}).items():
        if a not in out and now - v.get("seen_at", 0) <= keep:
            out[a] = dict(v, late=True)
    return out


def scan_heads(addresses, timeout=3.0):
    """Live: each head address that answers as Ollama from the hub, with its models (name, digest, size)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def one(address):
        try:
            with opener.open(f"http://{address}:{OLLAMA_PORT}/api/version", timeout=timeout) as r:
                version = json.loads(r.read(65536)).get("version")
            with opener.open(f"http://{address}:{OLLAMA_PORT}/api/tags", timeout=10) as r:
                tags = json.loads(r.read(4 << 20)).get("models", [])
            return address, {"version": version, "models": [{"name": m["name"], "digest": m.get("digest", ""), "size": m.get("size", 0)}
                                                             for m in tags if isinstance(m, dict) and m.get("name")]}
        except Exception:
            return address, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        return {a: v for a, v in pool.map(one, addresses) if v}


def elevate(python, script, args):
    """Ask Windows to start the helper as an administrator (the UAC prompt). True if it started."""
    import ctypes
    params = " ".join(['-I', f'"{script}"'] + list(args))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", python, params, str(Path(script).parent), 1)
    if rc <= 32:
        raise PermissionError("the administrator prompt was cancelled or refused; nothing was changed" if rc == 5
                              else f"Windows could not start the Fleet Commit helper (code {rc})")
    return True


class Fleet:
    def __init__(self, config, store, gateway_view, *, scanner=scan_heads, launcher=elevate, data_dir=None):
        f = config.get("fleet") or {}
        self.dir = Path(f.get("dir") or FLEET_DIR)
        self.helper, self.python = f.get("helper") or HELPER, f.get("python") or PYTHON
        self.store, self.gateway_view = store, gateway_view
        self.scanner, self.launcher = scanner, launcher
        self.data = Path(data_dir or Path(config["db_path"]).parent)
        self.lock = threading.Lock()
        self.scan, self.scan_at, self.scanning = {}, None, False
        self._catalog, self._catalog_sig = None, None

    # ------------------------------------------------------------------ files
    def _json(self, path, default):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, name, value):
        p = self.data / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def catalog(self):
        p = self.dir / "catalog" / "model-catalog.json"
        try:
            sig = p.stat().st_mtime_ns
        except OSError:
            return None
        if sig != self._catalog_sig:
            self._catalog, self._catalog_sig = self._json(p, None), sig
        return self._catalog

    def inventory(self):
        """{address: {ram_gb, vram_gb, gpu, computer}} from the lab's head list (written by the provisioner)."""
        doc = self._json(self.dir / "catalog" / "head-inventory.json", None)
        if not isinstance(doc, dict) or doc.get("schema") != "agent-arena.head-inventory/1":
            return {}
        return {h["address"]: h for h in doc.get("heads", []) if isinstance(h, dict) and head_address(h.get("address"))}

    def published(self):
        doc = self._json(self.dir / "published" / "fleet-state.json", None)
        return doc if isinstance(doc, dict) and doc.get("schema") == STATE_SCHEMA else None

    # ------------------------------------------------------------------ scan
    def scan_now(self, wait=False):
        with self.lock:
            if self.scanning:
                return False
            self.scanning = True
        known = {h["address"] for h in self.store.registry()["heads"] if head_address(h.get("address"))}

        def work():
            try:
                found = self.scanner(sorted(set(HEADS) | known, key=lambda a: int(a.rsplit(".", 1)[1])))
                now = time.time()
                with self.lock:
                    self.scan, self.scan_at = merge_scan(self.scan, found, now), now
            finally:
                self.scanning = False
        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
        return True

    def start_background(self):
        def loop():
            while True:
                try:
                    self.scan_now(wait=True)
                except Exception:
                    pass
                time.sleep(SCAN_EVERY)
        threading.Thread(target=loop, daemon=True).start()

    # ------------------------------------------------------------------ pending changes and Commit
    def pending(self):
        return self._json(self.data / "fleet-pending.json", {"changes": [], "commit": None})

    def set_pending(self, changes):
        if not isinstance(changes, list) or len(changes) > 200:
            raise ValueError("up to 200 changes in one Commit")
        clean, seen = [], set()
        for c in changes:
            c = check_change(c)
            k = change_key(c)
            if k in seen:
                continue
            seen.add(k)
            clean.append(c)
        doc = self.pending()
        if doc.get("commit") and (self.commit_status(doc["commit"]) or {}).get("state") in ("checking", "waiting-approval", "running"):
            raise ValueError("a Commit is still running; wait for it to finish")
        doc = {"changes": clean, "commit": None}
        self._save("fleet-pending.json", doc)
        return doc

    def commit(self, who="Arena"):
        doc = self.pending()
        if not doc["changes"]:
            raise ValueError("nothing to commit")
        if doc.get("commit") and (self.commit_status(doc["commit"]) or {}).get("state") in ("checking", "waiting-approval", "running"):
            raise ValueError("a Commit is still running")
        if not Path(self.helper).is_file():
            raise ValueError(f"the Fleet Commit helper is not installed ({self.helper}): run the provisioner's Administration hub > 5 as LabOperator1")
        rid = secrets.token_hex(8)
        req = self.dir / "requests" / f"{rid}.json"
        try:
            req.write_text(json.dumps({"schema": REQUEST_SCHEMA, "id": rid, "created_at": time.time(), "created_by": who,
                                       "changes": doc["changes"]}, indent=1), encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"the admin hub may not write the Commit request ({exc}); the provisioner's Administration hub > 5 sets up the folder") from None
        try:
            self.launcher(self.python, self.helper, ["--commit", rid])
        except PermissionError as exc:
            req.unlink(missing_ok=True)
            raise ValueError(str(exc)) from None
        doc["commit"], doc["settled"] = rid, False
        self._save("fleet-pending.json", doc)
        return {"id": rid, "changes": len(doc["changes"])}

    def commit_status(self, rid):
        if not rid or not re.fullmatch(r"[a-f0-9]{8,32}", rid):
            return None
        return self._json(self.dir / "status" / f"{rid}.json", None)

    def set_tier(self, address, tier):
        a = head_address(address)
        if not a:
            raise ValueError("not a head address")
        cat = self.catalog() or {"tiers": {}}
        if tier and tier not in cat["tiers"]:
            raise ValueError("not a tier in the catalog")
        overrides = self._json(self.data / "fleet-tiers.json", {})
        if tier:
            overrides[a] = tier
        else:
            overrides.pop(a, None)
        self._save("fleet-tiers.json", overrides)
        return {"address": a, "tier": tier}

    # ------------------------------------------------------------------ the view
    def view(self, now=None):
        now = now or time.time()
        cat = self.catalog() or {"tiers": {}, "models": []}
        tiers = cat.get("tiers") or {}
        entries = {m["name"]: m for m in cat.get("models", []) if m.get("runtime") == "ollama" and m.get("name")}
        pub = self.published() or {}
        gw = self.gateway_view(now) or {}
        gw_heads = {h["resource_id"]: h for h in gw.get("heads", [])} if isinstance(gw.get("heads"), list) else {}
        pub_heads = {h["address"]: h for h in pub.get("heads", [])}
        blocked = {}
        for b in pub.get("do_not_use", []):
            a = "192.0.2." + b["resource_id"].rsplit("-", 1)[1]
            blocked.setdefault(a, []).append(b["model"])
        registry = {h["address"]: h for h in self.store.registry()["heads"] if head_address(h.get("address"))}
        overrides = self._json(self.data / "fleet-tiers.json", {})
        inventory = self.inventory()
        with self.lock:
            scan, scan_at, scanning = dict(self.scan), self.scan_at, self.scanning
        addresses = sorted(set(registry) | set(scan) | set(pub_heads), key=lambda a: int(a.rsplit(".", 1)[1]))
        heads = []
        for a in addresses:
            reg, live, ph = registry.get(a), scan.get(a), pub_heads.get(a)
            rid = "head-" + a.replace(".", "-")
            obs = gw_heads.get(rid) or {}
            tele = (reg or {}).get("telemetry")
            tier = tier_for(tele, tiers, overrides.get(a), inventory.get(a))
            installed = live["models"] if live else []
            names = {m["name"] for m in installed}
            plan_missing, too_large = [], []
            if tier["id"]:
                plan_missing = sorted(n for n, m in entries.items() if (m.get("tiers") or {}).get(tier["id"]) == "YES" and n not in names)
                too_large = sorted(n for n in names if (entries.get(n, {}).get("tiers") or {}).get(tier["id"]) == "NO")
            heads.append({
                "address": a, "resource_id": rid, "name": (reg or {}).get("display_name") or f"Head {a.rsplit('.', 1)[1]}",
                "in_arena": bool(reg), "new": bool(live) and not reg and not ph,
                "arena_report_age": (now - tele["received_at"]) if tele else None,
                "tier": tier, "reachable": bool(live), "late": bool(live and live.get("late")),
                "seen_at": (live or {}).get("seen_at"), "ollama": (live or {}).get("version"),
                "models": sorted(installed, key=lambda m: m["name"]),
                "gateway": {"in_gateway": bool(ph or obs), "health": obs.get("health") or (ph or {}).get("health"),
                            "fresh": obs.get("fresh"), "qualified": sorted(obs.get("qualified_models") or (ph or {}).get("qualified") or []),
                            "applications": (ph or {}).get("applications", [])},
                "do_not_use": sorted(blocked.get(a, [])), "planned_missing": plan_missing, "too_large": too_large})
        gw_apps = {x["app_id"]: x for x in pub.get("applications", [])}
        arena_apps = {x["app_id"]: x["display_name"] for x in self.store.registry()["applications"]}
        projects = []
        for app_id in sorted(set(gw_apps) | set(KNOWN_PROJECTS) | set(arena_apps), key=lambda x: (x not in KNOWN_PROJECTS, x)):
            g = gw_apps.get(app_id)
            projects.append({"app_id": app_id, "name": KNOWN_PROJECTS.get(app_id) or arena_apps.get(app_id) or app_id,
                             "in_arena": app_id in arena_apps, "in_gateway": bool(g), "enabled": bool(g and g.get("enabled")),
                             "models": (g or {}).get("models", []),
                             "heads": [h["address"] for h in heads if app_id in h["gateway"]["applications"]],
                             "note": "" if g else "not connected yet: no Gateway login (the project has not delivered its client)"})
        pending = self.pending()
        status = self.commit_status(pending.get("commit"))
        if status and status.get("state") in ("done", "failed") and not pending.get("settled"):
            # What completed leaves the list; what did not stays, to fix and Commit again.
            pending = {"changes": still_to_do(pending["changes"], status), "commit": pending["commit"], "settled": True}
            self._save("fleet-pending.json", pending)
        answering = sum(1 for h in heads if h["reachable"])
        catalog_rows = [{"name": m["name"], "capability": m.get("capability"), "download_gb": m.get("download_gb"), "ram_gb": m.get("ram_gb"),
                         "vram_gb": m.get("vram_gb"), "tiers": m.get("tiers") or {}, "retention": m.get("retention"),
                         "license": m.get("license"), "verified": bool((m.get("verified") or {}).get("ok")),
                         "chat": not NOT_CHAT.search(m["name"] + " " + str(m.get("capability") or ""))}
                        for m in sorted(entries.values(), key=lambda m: m["name"])]
        library = {i["name"]: i for i in pub.get("library", []) if i.get("complete")}
        return {
            "time": now, "heads": heads, "projects": projects,
            "catalog": {"tiers": {k: v["label"] for k, v in tiers.items()}, "models": catalog_rows, "imported_at": cat.get("imported_at"),
                        "other_runtime": sum(1 for m in cat.get("models", []) if m.get("runtime") != "ollama"), "present": bool(self._catalog)},
            "library": sorted(library), "failed_tests": pub.get("failed_tests", []),
            "failures": {f"{f['resource_id']} {f['model']}": {k: f.get(k) for k in ("kind", "reason", "at")}
                         for f in pub.get("failures", []) if isinstance(f, dict) and f.get("resource_id") and f.get("model")},
            "published": {k: pub.get(k) for k in ("written_at", "written_by", "gateway", "last_commit")} if pub else None,
            "pending": pending["changes"], "commit": {"id": pending.get("commit"), "status": status} if pending.get("commit") else None,
            "scan": {"at": scan_at, "running": scanning},
            "health": {"heads": len(heads), "answering": answering, "in_gateway": sum(1 for h in heads if h["gateway"]["in_gateway"]),
                       "new": sum(1 for h in heads if h["new"]), "gateway": gw.get("state"), "pending": len(pending["changes"])},
            "helper_installed": Path(self.helper).is_file()}
