"""Fleet Commit (the admin hub's elevated helper): request checks, order, which tests run, a whole Commit
against stand-ins for the Gateway tools and the heads, and (with GATEWAY_RELEASE) that changing a head's
projects through the Gateway's own heads tool and bootstrap keeps its qualified models."""
import io
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "gateway-models"))
sys.path.insert(0, str(TOOLS / "model-library"))
import gateway_models as gm  # noqa: E402
import model_library as ml  # noqa: E402
import fleet_commit as fc  # noqa: E402

RELEASE = os.environ.get("GATEWAY_RELEASE", "")
APPS = [{"app_id": "capstone", "enabled": True, "models": ["qwen3.5:4b"], "max_concurrency": 1, "queue_limit": 16, "priority": 6,
         "operations": ["/api/chat", "/api/generate"], "max_context": None},
        {"app_id": "capstone-research", "enabled": True, "models": ["qwen3.5:4b", "gemma3:4b"], "max_concurrency": 1, "queue_limit": 32,
         "priority": 2, "operations": ["/api/chat", "/api/generate"], "max_context": None}]
IDS = {"capstone", "capstone-research"}


def req(*changes):
    return {"schema": fc.REQUEST_SCHEMA, "id": "abcdef12", "created_by": "test", "changes": list(changes)}


def test_requests_are_checked_as_untrusted():
    ok, problems = fc.validate(req({"op": "install", "address": "192.0.2.6", "model": "smollm:135m"},
                                   {"op": "allow", "app": "capstone", "model": "gemma3:4b"},
                                   {"op": "head-projects", "address": "192.0.2.3", "applications": ["capstone-research", "capstone"]}), apps=IDS)
    assert not problems
    assert [c["op"] for c in ok] == ["allow", "head-projects", "install"], "policy, then heads, then installs"
    assert ok[1]["applications"] == ["capstone", "capstone-research"]
    bad = [{"op": "install", "address": "192.0.2.254", "model": "x:y"},           # the hub is not a head
           {"op": "install", "address": "192.0.2.6", "model": "rm -rf /"},
           {"op": "allow", "app": "coder", "model": "llama3.2:3b"},              # not a Gateway project yet
           {"op": "head-projects", "address": "192.0.2.3", "applications": []},
           {"op": "do-not-use", "address": "192.0.2.3", "model": "gemma3:4b", "on": "yes"},
           {"op": "format-disk", "address": "192.0.2.3"}]
    for b in bad:
        assert fc.validate(req(b), apps=IDS)[1], b
    assert fc.validate({"changes": [bad[0]]}, apps=IDS)[1] == ["not a fleet request from the admin hub"]
    both = fc.validate(req({"op": "install", "address": "192.0.2.6", "model": "a:b"}, {"op": "uninstall", "address": "192.0.2.6", "model": "a:b"}), apps=IDS)
    assert any("both added and removed" in p for p in both[1])
    twice = fc.validate(req({"op": "install", "address": "192.0.2.6", "model": "a:b"}, {"op": "install", "address": "192.0.2.6", "model": "a:b"}), apps=IDS)
    assert any("listed twice" in p for p in twice[1])
    assert fc.validate(req(*([{"op": "install", "address": "192.0.2.6", "model": f"m{i}:1"} for i in range(201)])), apps=IDS)[1]


def test_descriptions_say_what_happens():
    assert fc.describe({"op": "uninstall", "address": "192.0.2.6", "model": "gemma3:4b"}) == "Uninstall gemma3:4b from head 6 (out of the Gateway first)"
    assert fc.describe({"op": "do-not-use", "address": "192.0.2.6", "model": "m:1", "on": False}) == "Use m:1 on head 6 again"


def test_the_admin_hub_uses_the_helpers_words():
    """Arena keeps a change in its list until the helper reports it done, by its description: they must match."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from arena_service import fleet as arena_fleet
    for c in ({"op": "allow", "app": "capstone", "model": "m:1"}, {"op": "disallow", "app": "capstone", "model": "m:1"},
              {"op": "head-projects", "address": "192.0.2.7", "applications": ["capstone", "capstone-research"]},
              {"op": "do-not-use", "address": "192.0.2.7", "model": "m:1", "on": True}, {"op": "do-not-use", "address": "192.0.2.7", "model": "m:1", "on": False},
              {"op": "install", "address": "192.0.2.7", "model": "hf.co/a/b:Q4"}, {"op": "uninstall", "address": "192.0.2.7", "model": "m:1"},
              {"op": "test-missing"}):
        assert arena_fleet.describe(c) == fc.describe(c), c
        assert arena_fleet.check_change(c) == {k: v for k, v in c.items()}, c


def test_test_missing_tests_every_allowed_unusable_pair_and_redoes_timeouts():
    """1.5.2: one change that tests what is not usable yet on every Gateway head (heads 2 and 10 after the 90 s timeouts)."""
    changes, problems = fc.validate({"schema": fc.REQUEST_SCHEMA, "changes": [{"op": "test-missing"}]}, apps={"capstone"})
    assert problems == [] and changes == [{"op": "test-missing"}]
    models = {"capstone": ["qwen3.5:4b", "gemma3:4b", "all-minilm:latest"], "capstone-research": ["qwen3.5:4b"]}
    head_apps = {"head-192-0-2-2": ["capstone"], "head-192-0-2-10": ["capstone", "capstone-research"], "head-192-0-2-7": ["capstone-research"]}
    installed = {"head-192-0-2-2": {"qwen3.5:4b", "gemma3:4b"}, "head-192-0-2-10": {"qwen3.5:4b", "gemma3:4b", "all-minilm:latest"},
                 "head-192-0-2-7": {"qwen3.5:4b"}}
    pairs = fc.tests_wanted([{"op": "test-missing"}], models=models, head_apps=head_apps, installed=installed,
                            qualified={"head-192-0-2-7": {"qwen3.5:4b"}, "head-192-0-2-10": {"gemma3:4b"}},
                            blocked={("head-192-0-2-2", "gemma3:4b")}, failed_before={("head-192-0-2-10", "qwen3.5:4b")})
    assert pairs == [("head-192-0-2-10", "all-minilm:latest"), ("head-192-0-2-2", "qwen3.5:4b")]
    # judged: a real answer, or a lasting refusal; a timeout while loading is not a verdict
    assert fc.judged({"response": {"response": "Ready."}, "error": None}) is True
    assert fc.judged({"error": "Metadata does not support qualification operation"}) is True
    assert fc.judged({"error": "Qualification deadline expired"}) is False
    assert fc.judged({"error": "<urlopen error timed out>"}) is False
    assert fc.judged(None) is False


def test_ledger_evidence_reads_the_gateways_own_rows(tmp_path):
    db = tmp_path / "ledger.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE capacity_qualification_evidence(evidence_id TEXT PRIMARY KEY, resource_id TEXT, model TEXT, digest TEXT, version TEXT, passed INTEGER, evidence TEXT, at REAL)")
    con.execute("INSERT INTO capacity_qualification_evidence VALUES('E1','head-192-0-2-10','qwen3:14b','d','v',0,?,1)", (json.dumps({"error": "Qualification deadline expired"}),))
    con.execute("INSERT INTO capacity_qualification_evidence VALUES('E2','head-192-0-2-2','llama3.1:8b','d','v',0,?,2)", (json.dumps({"response": {"response": "READY."}}),))
    con.commit(); con.close()
    got = fc.ledger_evidence(db, ["E1", "E2", None, "E3"])
    assert set(got) == {"E1", "E2"} and fc.judged(got["E1"]) is False and fc.judged(got["E2"]) is True
    assert fc.ledger_evidence(db, []) == {}


def test_models_after_only_changes_what_was_asked():
    models, changed = fc.models_after(APPS, [{"op": "allow", "app": "capstone", "model": "gemma3:4b"},
                                             {"op": "disallow", "app": "capstone-research", "model": "nothere:1"}])
    assert models["capstone"] == ["gemma3:4b", "qwen3.5:4b"] and changed == ["capstone"]
    assert models["capstone-research"] == ["gemma3:4b", "qwen3.5:4b"]


def test_tests_wanted():
    models = {"capstone": ["qwen3.5:4b", "gemma3:4b"], "capstone-research": ["qwen3.5:4b", "nomic-embed-text:latest"]}
    head_apps = {"head-192-0-2-3": ["capstone"], "head-192-0-2-6": ["capstone-research"]}
    installed = {"head-192-0-2-3": {"qwen3.5:4b", "gemma3:4b"}, "head-192-0-2-6": {"qwen3.5:4b", "gemma3:4b", "nomic-embed-text:latest"}}
    pairs = fc.tests_wanted([{"op": "allow", "app": "capstone", "model": "gemma3:4b"}], models=models, head_apps=head_apps, installed=installed,
                            qualified={"head-192-0-2-3": {"qwen3.5:4b"}}, blocked=set(), failed_before=set())
    assert pairs == [("head-192-0-2-3", "gemma3:4b")], "head 6 serves only research, which does not allow gemma3:4b"
    assert fc.tests_wanted([{"op": "allow", "app": "capstone", "model": "gemma3:4b"}], models=models, head_apps=head_apps, installed=installed,
                           qualified={}, blocked={("head-192-0-2-3", "gemma3:4b")}, failed_before=set()) == []
    # a failed test is not repeated by an unrelated change, but is after a fresh install
    failed = {("head-192-0-2-3", "gemma3:4b")}
    assert fc.tests_wanted([{"op": "allow", "app": "capstone", "model": "gemma3:4b"}], models=models, head_apps=head_apps, installed=installed,
                           qualified={}, blocked=set(), failed_before=failed) == []
    assert fc.tests_wanted([{"op": "install", "address": "192.0.2.3", "model": "gemma3:4b"}], models=models, head_apps=head_apps,
                           installed=installed, qualified={}, blocked=set(), failed_before=failed) == [("head-192-0-2-3", "gemma3:4b")]
    hp = fc.tests_wanted([{"op": "head-projects", "address": "192.0.2.6", "applications": ["capstone-research"]}], models=models,
                         head_apps=head_apps, installed=installed, qualified={}, blocked=set(), failed_before=set())
    # 1.5.0: an allowed embedding model is tested too (Capstone 6.0.0-rc.4's retrieval needs one), with the
    # Gateway's embedding test, not the chat test (see test_qualify_purpose).
    assert hp == [("head-192-0-2-6", "nomic-embed-text:latest"), ("head-192-0-2-6", "qwen3.5:4b")]


def test_qualify_purpose(tmp_path):
    """The Gateway's qualify gets --purpose embedding for an embedding model and --purpose completion otherwise."""
    calls = []
    tools = gm.GatewayTools.__new__(gm.GatewayTools)
    tools.evidence_dir, tools.ledger, tools.operator = tmp_path, tmp_path / "ledger.db", "test"
    tools.run = lambda *args, **kw: calls.append(args) or {"ok": True}
    for model in ("all-minilm:latest", "granite-embedding:30m", "nomic-embed-text:latest", "bge-m3", "qwen3.5:4b", "gemma3:4b"):
        tools.qualify("head-192-0-2-3", model)
    purposes = [c[c.index("--purpose") + 1] for c in calls]
    assert purposes == ["embedding"] * 4 + ["completion"] * 2


# ---------------------------------------------------------------------------- a whole Commit with stand-ins

def make_ledger(path, heads):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE capacity_resources(resource_id TEXT, resource_type TEXT, applications TEXT)")
    for rid, apps in heads.items():
        db.execute("INSERT INTO capacity_resources VALUES(?,?,?)", (rid, "shared_inference", json.dumps(apps)))
    db.commit()
    db.close()


class Tools:
    def __init__(self, ledger):
        self.ledger, self.release, self.operator = Path(ledger), Path("0.3.0"), "tester"
        self.calls, self.apps = [], [dict(a) for a in APPS]
        self.qualified = {"head-192-0-2-3": ["qwen3.5:4b"], "head-192-0-2-6": []}

    def applications(self):
        return self.apps

    def snapshot(self):
        return {"ledger_id": "L1", "admission_open": True,
                "resources": [{"resource_id": rid, "resource_type": "shared_inference", "health": "healthy", "qualified_models": list(q)}
                              for rid, q in self.qualified.items()],
                "qualification_observations": []}

    def bootstrap(self, f):
        doc = json.loads(Path(f).read_text())
        self.calls.append(("bootstrap", doc))
        for c in doc.get("clients", []):
            next(a for a in self.apps if a["app_id"] == c["app_id"])["models"] = c["models"]
        if doc.get("resources"):
            db = sqlite3.connect(self.ledger)
            for r in doc["resources"]:
                db.execute("DELETE FROM capacity_resources WHERE resource_id=?", (r["resource_id"],))
                db.execute("INSERT INTO capacity_resources VALUES(?,?,?)", (r["resource_id"], "shared_inference", json.dumps(r["applications"])))
            db.commit()
            db.close()
        return {"ok": True, "exit": 0, "json": {}, "error": None}

    def heads(self, inv, base, applications, output):
        self.calls.append(("heads", list(applications)))
        inv = json.loads(Path(inv).read_text())
        h = inv["heads"][0]
        Path(output).write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [],
                                            "resources": [{"resource_id": h["resource_id"], "endpoint": h["endpoint"], "resource_type": "shared_inference",
                                                           "owner": "infrastructure", "applications": sorted(applications), "permitted_uses": ["inference"]}]}))
        return {"ok": True, "exit": 0, "json": {}, "error": None}

    def withdraw(self, rid, model):
        self.calls.append(("withdraw", rid, model))
        q = self.qualified.get(rid, [])
        if model in q:
            q.remove(model)
            return True
        return False

    def qualify(self, rid, model):
        self.calls.append(("qualify", rid, model))
        self.qualified.setdefault(rid, []).append(model)
        return {"ok": True, "exit": 0, "json": {"passed": True}, "error": None}


class Heads:
    """Two heads' Ollama: a pull from the hub's library gives the library's digest."""

    def __init__(self, library_digest):
        self.models = {"192.0.2.3": {"qwen3.5:4b": "sha256:" + "a" * 64, "gemma3:4b": "sha256:" + "b" * 64},
                       "192.0.2.6": {"qwen3.5:4b": "sha256:" + "a" * 64}}
        self.digest, self.calls = library_digest, []

    def scan(self, addresses):
        return {a: {"version": "0.40.13", "models": dict(self.models[a])} for a in addresses if a in self.models}

    def http(self, address, path, body, timeout=30, method=None):
        self.calls.append((address, path, body))
        m = self.models[address]
        if path == "/api/pull":
            assert body["insecure"] is True and body["model"].startswith("192.0.2.254:11500/")
            m[body["model"]] = self.digest
            return {"status": "success"}
        if path == "/api/copy":
            m[body["destination"]] = m[body["source"]]
        if path == "/api/delete":
            m.pop(body["model"], None)
        return {}

    def tags(self, address):
        return dict(self.models[address])


class Library:
    def __init__(self):
        self.fetched = []

    def index(self):
        return [{"name": n, "served": "library/x:1", "digest": "sha256:" + "c" * 64, "size": 92_000_000, "complete": True,
                 "fetched_at": "now"} for n in self.fetched]

    def fetch(self, name, say=print):
        self.fetched.append(name)
        return {}


@pytest.fixture()
def fleet(tmp_path):
    ledger = tmp_path / "ledger.db"
    make_ledger(ledger, {"head-192-0-2-3": ["capstone", "capstone-research"], "head-192-0-2-6": ["capstone-research"]})
    tools = Tools(ledger)
    console = gm.Console(tools, tmp_path / "console", scanner=lambda: {})
    heads, lib = Heads("sha256:" + "c" * 64), Library()
    served = []
    f = fc.Fleet(tools, console, lib, tmp_path / "fleet", scanner=heads.scan, http=heads.http, tags=heads.tags,
                 serve=lambda a: served.append(sorted(a)) or None, firewall=lambda a, on: served.append(("firewall", on)))
    (tmp_path / "fleet" / "requests").mkdir(parents=True)
    return f, tools, heads, lib, served, tmp_path


def write_request(tmp_path, *changes, rid="abcdef12"):
    (tmp_path / "fleet" / "requests" / f"{rid}.json").write_text(json.dumps(req(*changes)), encoding="utf-8")
    return rid


def test_a_commit_runs_everything_in_order_and_publishes(fleet):
    f, tools, heads, lib, served, tmp = fleet
    rid = write_request(tmp, {"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"},
                        {"op": "allow", "app": "capstone-research", "model": "smollm2:135m"},
                        {"op": "uninstall", "address": "192.0.2.3", "model": "gemma3:4b"},
                        {"op": "head-projects", "address": "192.0.2.6", "applications": ["capstone", "capstone-research"]})
    assert fc.commit(f, rid, confirm=lambda _: "APPROVE") == 0
    st = json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())
    assert st["state"] == "done" and st["ok"] and all(r["ok"] for r in st["results"]), st
    kinds = [c[0] for c in tools.calls]
    assert kinds.index("bootstrap") < kinds.index("heads") < kinds.index("withdraw") < kinds.index("qualify")
    assert ("withdraw", "head-192-0-2-3", "gemma3:4b") in tools.calls, "out of the Gateway before it leaves the head"
    assert "gemma3:4b" not in heads.models["192.0.2.3"]
    assert heads.models["192.0.2.6"]["smollm2:135m"] == "sha256:" + "c" * 64 and not any(k.startswith("192.0.2.254") for k in heads.models["192.0.2.6"])
    assert lib.fetched == ["smollm2:135m"], "downloaded once"
    assert served == [("firewall", True), ["192.0.2.6"], ("firewall", False)], "the library is open to the target heads only while installing"
    assert ("qualify", "head-192-0-2-6", "smollm2:135m") in tools.calls
    pub = json.loads((tmp / "fleet" / "published" / "fleet-state.json").read_text())
    assert pub["schema"] == fc.STATE_SCHEMA and pub["last_commit"]["ok"]
    assert next(h for h in pub["heads"] if h["address"] == "192.0.2.6")["applications"] == ["capstone", "capstone-research"]
    assert "smollm2:135m" in next(a for a in pub["applications"] if a["app_id"] == "capstone-research")["models"]
    text = json.dumps(pub).lower()
    assert "token" not in text and "11434" not in text, "nothing secret, no endpoints"
    assert (tmp / "console" / "commits" / f"{rid}.json").is_file(), "the helper works from its own copy"


def test_nothing_happens_without_approve_or_with_a_bad_request(fleet):
    f, tools, heads, lib, served, tmp = fleet
    rid = write_request(tmp, {"op": "uninstall", "address": "192.0.2.3", "model": "gemma3:4b"})
    assert fc.commit(f, rid, confirm=lambda _: "yes") == 1
    assert tools.calls == [] and "gemma3:4b" in heads.models["192.0.2.3"]
    assert json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())["state"] == "declined"
    rid = write_request(tmp, {"op": "uninstall", "address": "192.0.2.254", "model": "gemma3:4b"}, rid="0badc0de")
    assert fc.commit(f, rid, confirm=lambda _: "APPROVE") == 2
    assert json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())["state"] == "refused"
    assert fc.commit(f, "../../x", confirm=lambda _: "APPROVE") == 2


def test_a_refused_policy_stops_the_commit(fleet):
    f, tools, heads, lib, served, tmp = fleet
    tools.bootstrap = lambda p: {"ok": False, "exit": 1, "json": None, "error": "no"}
    rid = write_request(tmp, {"op": "allow", "app": "capstone", "model": "gemma3:4b"}, {"op": "uninstall", "address": "192.0.2.3", "model": "gemma3:4b"})
    assert fc.commit(f, rid, confirm=lambda _: "APPROVE") == 1
    assert "gemma3:4b" in heads.models["192.0.2.3"], "nothing after a refused policy"
    assert json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())["state"] == "failed"


def test_a_download_failure_fails_only_that_install(fleet):
    f, tools, heads, lib, served, tmp = fleet
    lib.fetch = lambda name, say=print: (_ for _ in ()).throw(OSError("no internet"))
    rid = write_request(tmp, {"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"}, {"op": "uninstall", "address": "192.0.2.3", "model": "gemma3:4b"})
    assert fc.commit(f, rid, confirm=lambda _: "APPROVE") == 1
    st = json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())
    res = {r["change"]: r for r in st["results"]}
    assert not res["Install smollm2:135m on head 6 (an exact copy from the hub's library)"]["ok"]
    assert "Wi-Fi" in res["Install smollm2:135m on head 6 (an exact copy from the hub's library)"]["detail"]
    assert res["Uninstall gemma3:4b from head 3 (out of the Gateway first)"]["ok"]
    assert served == [], "the library was never opened"


def test_the_helper_starts_on_its_own_as_windows_starts_it(tmp_path):
    """python -I, nothing preloaded: its sibling modules load from its own folder (a lost line once broke this
    while every in-process test still passed)."""
    import shutil
    import subprocess
    for f in ("fleet_commit.py", "gateway_models.py"):
        shutil.copy2(TOOLS / "gateway-models" / f, tmp_path / f)
    shutil.copy2(TOOLS / "model-library" / "model_library.py", tmp_path / "model_library.py")   # as installed: one folder
    p = subprocess.run([sys.executable, "-I", str(tmp_path / "fleet_commit.py"), "--rehearsal", "--ledger", str(tmp_path / "none.db"),
                        "--release", str(tmp_path), "--publish"], capture_output=True, text=True, timeout=60)
    assert "Traceback" not in p.stderr, p.stderr
    assert "ledger is not at" in p.stdout + p.stderr, "it reached its own checks"


# ---------------------------------------------------------------------------- step 6: warm-up, unlock, test (field 2026-11-08)

class GatewayStandIn:
    """The Gateway 0.3.0's qualify and reconcile as they behave: a quarantined head refuses qualify until reconciled,
    reconcile refuses while the head has an active reservation, the completion check wants exactly READY."""

    def __init__(self, events, answers, health=None, leases=(), reconcile_refusal=None):
        self.events, self.answers = events, answers          # answers: {(address, model): text} the heads give
        self.health = dict(health or {})
        self.leases, self.reconcile_refusal = list(leases), reconcile_refusal
        self.operator, self.lock = "tester", threading.Lock()

    def snapshot(self):
        return {"resources": [{"resource_id": rid, "resource_type": "shared_inference", "health": h} for rid, h in self.health.items()],
                "leases": [{"resource_id": rid, "status": "leased"} for rid in self.leases]}

    def reconcile(self, rid):
        self.events.append(("reconcile", rid))
        if self.reconcile_refusal:
            return {"ok": False, "exit": 1, "json": None, "error": self.reconcile_refusal}
        self.health[rid] = "unqualified"
        return {"ok": True, "exit": 0, "json": {"resource_id": rid, "health": "unqualified"}, "error": None}

    def qualify(self, rid, model):
        self.events.append(("qualify", rid, model))
        if self.health.get(rid) == "quarantined":
            return {"ok": False, "exit": 1, "json": None, "evidence_error": None,
                    "error": "PermissionError: Quarantined execution must be reconciled before qualification"}
        answer = self.answers.get((gm.head_address(rid), model), "READY")
        passed = (not gm.is_chat_model(model)) or answer.strip() == "READY"
        self.health[rid] = "healthy" if passed else "degraded"
        return {"ok": passed, "exit": 0 if passed else 1, "json": {"passed": passed}, "evidence_error": None, "error": ""}


class OllamaStandIn:
    """The heads' Ollama for warm-ups: an answer, an HTTP error with Ollama's body, or a hook (for timing tests)."""

    def __init__(self, events, answers, errors=None, hook=None):
        self.events, self.answers, self.errors, self.hook = events, answers, errors or {}, hook
        self.bodies = []

    def __call__(self, address, path, body, timeout=30, method=None):
        self.events.append(("warm", address, body["model"]))
        self.bodies.append((address, path, body, timeout))
        if self.hook:
            self.hook(address, body["model"])
        err = self.errors.get((address, body["model"]))
        if err:
            code, text = err
            raise urllib.error.HTTPError(f"http://{address}:11434{path}", code, "Bad Request", {}, io.BytesIO(json.dumps({"error": text}).encode()))
        if path == "/api/embed":
            return {"model": body["model"], "embeddings": [[0.1, 0.2, 0.3]]}
        return {"model": body["model"], "response": self.answers.get((address, body["model"]), "READY"), "done": True}


def run_tests(pairs, answers=None, errors=None, hook=None, **gateway):
    events, lines = [], []
    answers = answers or {}
    tools = GatewayStandIn(events, answers, **gateway)
    http = OllamaStandIn(events, answers, errors, hook)
    results = gm.run_head_tests(tools, pairs, http=http, say=lines.append)
    return results, events, lines, http


H3, H10 = "head-192-0-2-3", "head-192-0-2-10"


def test_each_model_is_loaded_on_its_head_before_the_gateway_tests_it():
    results, events, lines, http = run_tests([(H10, "qwen3.5:4b"), (H10, "gemma3:4b")])
    assert events == [("warm", "192.0.2.10", "qwen3.5:4b"), ("qualify", H10, "qwen3.5:4b"),
                      ("warm", "192.0.2.10", "gemma3:4b"), ("qualify", H10, "gemma3:4b")]
    address, path, body, timeout = http.bodies[0]
    # Exactly the Gateway's own request, so the model the test finds loaded is the one it needs (num_ctx = --max-context).
    assert path == "/api/generate" and timeout == 900
    assert body == {"model": "qwen3.5:4b", "prompt": "Return exactly the word READY.", "stream": False, "think": False, "keep_alive": "10m",
                    "options": {"num_ctx": 8192, "num_predict": 24, "temperature": 0, "seed": 42}}
    assert body["options"]["num_ctx"] == gm.QUALIFY_CONTEXT
    assert all(r["passed"] and r["kind"] == "usable" for r in results.values())
    assert any(l.startswith("  qwen3.5:4b on head 10: loading on the head (took ") for l in lines)
    assert "  gemma3:4b on head 10: passed, usable now" in lines
    assert lines[-1] == "head 10: 2 usable"


def test_an_answer_other_than_exactly_ready_is_reported_as_the_gateways_rule():
    long = "<think>\nOkay, the user wants me to return exactly the word READY. Let me think about this carefully.\n"
    results, events, lines, _ = run_tests([(H10, "llama3.1:8b"), (H10, "qwen3:4b"), (H10, "qwen3.5:4b")],
                                          answers={("192.0.2.10", "llama3.1:8b"): "Ready.", ("192.0.2.10", "qwen3:4b"): long})
    assert ("qualify", H10, "llama3.1:8b") in events, "the Gateway still runs its own test (it returns cleanly)"
    r = results[(H10, "llama3.1:8b")]
    assert not r["passed"] and r["kind"] == "exact-ready"
    assert ('  llama3.1:8b on head 10: FAILED: it answered "Ready." and the Gateway accepts only exactly READY '
            "(the model works; the Gateway's test refuses it)") in lines
    q = next(l for l in lines if l.startswith("  qwen3:4b on head 10: FAILED"))
    shown = q.split('answered "', 1)[1].split('"', 1)[0]
    assert "\n" not in shown and len(shown) <= 60 and shown.startswith("<think> Okay,") and shown.endswith("...")
    assert lines[-1] == "head 10: 1 usable, 2 refused by the Gateway's exact-READY rule (llama3.1:8b, qwen3:4b)"


def test_a_locked_head_is_unlocked_after_the_warm_up_then_tested():
    results, events, lines, _ = run_tests([(H10, "qwen3.5:4b"), (H10, "gemma3:4b")], health={H10: "quarantined"})
    assert events[:3] == [("warm", "192.0.2.10", "qwen3.5:4b"), ("reconcile", H10), ("qualify", H10, "qwen3.5:4b")], "idle first, then unlock"
    assert events.count(("reconcile", H10)) == 1
    assert "  head 10 was locked by an earlier timed-out test; unlocked after checking it is idle" in lines
    assert all(r["passed"] for r in results.values())


def test_a_head_locked_during_the_run_is_unlocked_when_the_gateway_says_so():
    """The health read can be stale: when the Gateway refuses with its quarantine text, unlock and test once more."""
    events, lines = [], []
    tools = GatewayStandIn(events, {}, health={H10: "quarantined"})
    tools.snapshot = lambda: {"resources": [], "leases": []}           # observe did not show it
    gm.run_head_tests(tools, [(H10, "qwen3.5:4b")], http=OllamaStandIn(events, {}), say=lines.append)
    assert events == [("warm", "192.0.2.10", "qwen3.5:4b"), ("qualify", H10, "qwen3.5:4b"), ("reconcile", H10), ("qualify", H10, "qwen3.5:4b")]
    assert "  qwen3.5:4b on head 10: passed, usable now" in lines


def test_a_refused_unlock_skips_the_rest_of_that_head_with_the_reason():
    results, events, lines, _ = run_tests([(H3, "qwen3.5:4b"), (H10, "qwen3.5:4b"), (H10, "gemma3:4b"), (H10, "llama3.2:3b")],
                                          health={H10: "quarantined"},
                                          reconcile_refusal="ValueError: Resource still has an active reservation")
    assert not any(e[0] == "qualify" and e[1] == H10 for e in events), "never tested while the Gateway keeps it locked"
    assert [e for e in events if e[0] == "warm" and e[1] == "192.0.2.10"] == [("warm", "192.0.2.10", "qwen3.5:4b")], "no more loads on it"
    for m in ("qwen3.5:4b", "gemma3:4b", "llama3.2:3b"):
        assert not results[(H10, m)]["passed"] and "busy with work; Commit again later" in results[(H10, m)]["why"]
    assert results[(H3, "qwen3.5:4b")]["passed"], "the other head is not affected"
    assert "  gemma3:4b on head 10: not tested (the head is busy with work; Commit again later)" in lines
    assert "head 10: 0 usable, 3 not tested (qwen3.5:4b, gemma3:4b, llama3.2:3b)" in lines


def test_a_head_busy_with_work_is_not_loaded_behind_the_gateways_back():
    results, events, lines, _ = run_tests([(H10, "qwen3.5:4b"), (H10, "gemma3:4b")], leases=[H10])
    assert events == [], "no warm-up on a head the Gateway has given to a job"
    assert all("busy with work" in r["why"] for r in results.values())


def test_a_model_that_does_not_load_is_not_given_to_the_gateway_with_ollamas_reason():
    results, events, lines, _ = run_tests([(H10, "bad-embed:1"), (H10, "qwen3.5:4b")],
                                          errors={("192.0.2.10", "qwen3.5:4b"): (400, '"qwen3.5:4b" does not support generate')})
    assert ("qualify", H10, "qwen3.5:4b") not in events, "it would only run out of time and lock the head"
    r = results[(H10, "qwen3.5:4b")]
    assert not r["passed"] and r["kind"] == "load"
    assert 'HTTP 400 "qwen3.5:4b" does not support generate' in r["why"]
    assert any(l.startswith("  qwen3.5:4b on head 10: FAILED: it could not be loaded on the head: HTTP 400") for l in lines)
    assert lines[-1] == "head 10: 1 usable, 1 failed to load (qwen3.5:4b)"


def test_a_warm_up_that_times_out_or_finds_no_head_says_so():
    assert gm.warmup_error(socket.timeout("timed out")) == "it did not finish loading within 15 minutes"
    assert gm.warmup_error(urllib.error.URLError(TimeoutError("timed out"))) == "it did not finish loading within 15 minutes"
    assert gm.warmup_error(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))).startswith("the head did not answer over the lab cable")
    assert gm.warmup_error(urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b"not json"))) == "HTTP 500 not json"


def test_an_embedding_model_warms_up_with_a_real_embed():
    results, events, lines, http = run_tests([(H10, "all-minilm:latest")])
    address, path, body, timeout = http.bodies[0]
    assert path == "/api/embed" and body == {"model": "all-minilm:latest", "input": ["Synthetic qualification fixture."], "keep_alive": "10m"}
    assert results[(H10, "all-minilm:latest")]["passed"]


def test_heads_test_side_by_side_but_one_model_at_a_time_on_each_head():
    barrier = threading.Barrier(2, timeout=5)        # both heads' first warm-ups must be in flight together
    busy, worst, guard = {}, {}, threading.Lock()

    def hook(address, model):
        with guard:
            busy[address] = busy.get(address, 0) + 1
            worst[address] = max(worst.get(address, 0), busy[address])
        try:
            if model == "qwen3.5:4b":
                barrier.wait()
            time.sleep(0.05)
        finally:
            with guard:
                busy[address] -= 1

    models = ["qwen3.5:4b", "gemma3:4b", "llama3.2:3b"]
    results, events, lines, _ = run_tests([(H3, m) for m in models] + [(H10, m) for m in models], hook=hook)
    assert all(r["passed"] for r in results.values()), [r["why"] for r in results.values()]
    assert worst == {"192.0.2.3": 1, "192.0.2.10": 1}, "never two loads at once on one head"
    for rid, address in ((H3, "192.0.2.3"), (H10, "192.0.2.10")):
        mine = [e for e in events if (e[0] == "warm" and e[1] == address) or (e[0] == "qualify" and e[1] == rid)]
        assert [e[0] for e in mine] == ["warm", "qualify"] * 3 and [e[2] for e in mine[::2]] == models, "in order on each head"
    assert lines[-2:] == ["head 3: 3 usable", "head 10: 3 usable"], "summaries in head order"


def test_pure_helpers_for_the_tests():
    assert gm.judge_warmup("READY") == (True, "") and gm.judge_warmup("  READY\n") == (True, "")
    assert gm.judge_warmup("READY.")[0] is False and 'answered "READY."' in gm.judge_warmup("READY.")[1]
    assert "empty answer" in gm.judge_warmup("")[1] and gm.judge_warmup("READY", done=False)[0] is False
    assert gm.one_line("a\n b\r\n\tc") == "a b c" and len(gm.one_line("x" * 200)) == 60
    assert list(gm.plan_head_tests([(H10, "a"), (H3, "b"), (H10, "c")]).items()) == [(H3, ["b"]), (H10, ["a", "c"])]
    assert gm.qualify_reason({"error": "ValueError: Qualification cannot interrupt allocated capacity"}, "x") == "the head was busy with work; Commit again later"
    assert gm.qualify_reason({"error": "", "json": {"passed": False}, "evidence_error": "Qualification deadline expired"}).startswith("the Gateway's test ran out of time")
    assert gm.qualify_reason({"error": "", "json": {"passed": False}, "evidence_error": "something odd"}) == "the Gateway's test says: something odd"
    assert gm.qualify_reason({"error": "", "json": {"passed": False}}) == "its answer did not pass the Gateway's check"
    assert gm.qualify_reason({"error": "", "json": None, "exit": 3}) == "exit 3"


def test_gateway_tools_unlock_and_read_the_tests_reason(tmp_path):
    calls = []
    tools = gm.GatewayTools.__new__(gm.GatewayTools)
    tools.evidence_dir, tools.ledger, tools.operator = tmp_path, tmp_path / "ledger.db", "LabOperator1"

    def run(*args, **kw):
        calls.append(args)
        if args[0] == "qualify":
            Path(args[args.index("--output") + 1]).write_text(json.dumps({"passed": False, "error": "Qualification deadline expired"}))
        return {"ok": False, "exit": 1, "json": {"passed": False}, "error": ""}
    tools.run = run
    tools.reconcile("head-192-0-2-10")
    assert calls[0] == ("lifecycle", "reconcile-resource", "--database", str(tmp_path / "ledger.db"), "--resource", "head-192-0-2-10",
                        "--operator", "LabOperator1", "--confirmed-idle")
    q = tools.qualify("head-192-0-2-10", "qwen3.5:4b")
    assert q["evidence_error"] == "Qualification deadline expired"
    assert calls[1][calls[1].index("--max-context") + 1] == "8192", "the Gateway call is unchanged"


def test_a_commit_reports_each_tests_real_reason_and_a_summary(fleet):
    f, tools, heads, lib, served, tmp = fleet
    answers = {"qwen3.5:4b": "READY", "gemma3:4b": "Ready."}

    def http(address, path, body, timeout=30, method=None):
        if path == "/api/generate":
            heads.calls.append((address, "warm", body["model"]))
            return {"response": answers.get(body["model"], "READY"), "done": True}
        return Heads.http(heads, address, path, body, timeout, method)
    f.http = http

    def qualify(rid, model):
        tools.calls.append(("qualify", rid, model))
        ok = answers.get(model, "READY") == "READY"
        return {"ok": ok, "exit": 0 if ok else 1, "json": {"passed": ok}, "error": "", "evidence_error": None}
    tools.qualify = qualify
    rid = write_request(tmp, {"op": "allow", "app": "capstone", "model": "gemma3:4b"})
    assert fc.commit(f, rid, confirm=lambda _: "APPROVE") == 1
    st = json.loads((tmp / "fleet" / "status" / f"{rid}.json").read_text())
    assert st["summary"] == "1 did not complete: test of gemma3:4b on head 3", "failures keep their words"
    text = "\n".join(st["lines"])
    assert ("192.0.2.3", "warm", "gemma3:4b") in heads.calls
    assert 'gemma3:4b on head 3: FAILED: it answered "Ready." and the Gateway accepts only exactly READY' in text
    assert "head 3: 0 usable, 1 refused by the Gateway's exact-READY rule (gemma3:4b)" in text
    log = [json.loads(l) for l in (tmp / "console" / "changes.log").read_text().splitlines()]
    q = next(e for e in log if e["action"] == "qualify (Fleet Commit)")
    assert q["passed"] is False and "exactly READY" in q["error"] and q["load_seconds"] is not None


def test_the_library_serves_heads_by_the_name_the_helper_pulls():
    host, ns, model, tag = ml.parse_name("hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M")
    assert f"{fc.HUB}:{fc.LIBRARY_PORT}/{ml.served_path(host, ns, model)}:{tag}" == "192.0.2.254:11500/hfco-unsloth/SmolLM3-3B-GGUF:Q4_K_M"


@pytest.mark.skipif(not (RELEASE and Path(RELEASE, "inference_gateway").is_dir()), reason="set GATEWAY_RELEASE to the Gateway 0.3.0 release folder")
def test_head_projects_through_the_real_gateway_keep_its_qualified_models(tmp_path):
    sys.path.insert(0, RELEASE)
    from inference_gateway.capacity_broker import CapacityBroker
    from inference_gateway.capacity_bootstrap import apply
    from inference_gateway.head_inventory import resources
    ledger = tmp_path / "ledger.db"
    b = CapacityBroker(str(ledger))
    b.register_client("capstone", "x" * 40, models=["qwen3.5:4b"], max_concurrency=1, queue_limit=16, priority=6)
    b.register_client("capstone-research", "y" * 40, models=["qwen3.5:4b"], max_concurrency=1, queue_limit=32, priority=2)
    b.authorize_resource("head-192-0-2-3", endpoint="http://192.0.2.3:11434", resource_type="shared_inference", owner="infrastructure",
                         applications=["capstone", "capstone-research"], permitted_uses=["inference"])
    b.publish("head-192-0-2-3", ["qwen3.5:4b"], "healthy")
    inv = gm.head_inventory("192.0.2.3", "0.40.13", [{"name": "qwen3.5:4b", "digest": "d"}], "TEST")
    accepted, refused = resources(inv, ["capstone-research"])
    assert not refused
    pf = tmp_path / "r.json"
    pf.write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [], "resources": accepted}))
    apply(str(pf), str(ledger))
    assert fc.ledger_heads(ledger) == {"head-192-0-2-3": ["capstone-research"]}
    with b.connect() as db:
        row = db.execute("SELECT qualified_models, health FROM capacity_resources WHERE resource_id='head-192-0-2-3'").fetchone()
    assert json.loads(row[0]) == ["qwen3.5:4b"] and row[1] == "healthy", "same endpoint: qualification kept"


def test_each_failure_says_why_and_whether_a_retest_can_change_it():
    # The model answered: the Gateway's exact-READY rule refused it (it works).
    kind, why = fc.failure_detail("smollm2:360m", {"response": {"response": "Ready.", "done": True}})
    assert kind == "rule" and '"Ready."' in why and "exactly READY" in why
    # A lasting refusal: a retest does not change it.
    kind, why = fc.failure_detail("qwen3:1.7b", {"error": "Metadata does not support completion"})
    assert kind == "lasting" and "/api/show" in why
    # Never judged: the head was locked, or the old test ran out of time before the warm-up existed.
    kind, why = fc.failure_detail("gemma3:4b", {"error": "PermissionError: Quarantined execution must be reconciled before qualification"})
    assert kind == "retest" and "locked" in why and "Test models not usable yet" in why
    kind, why = fc.failure_detail("qwen3:14b", None)
    assert kind == "retest" and "ran out of time" in why
