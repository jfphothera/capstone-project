import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from capstone_rag import experiments, gateway_transport, inference
from capstone_rag.ai_settings import Registry, ROLES, canonical
from capstone_rag.store import Store
from shared_inference_client import Client


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_SOURCE = ROOT.parent / "Inference Gateway" / "src"


class _Broker:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE capacity_resources(resource_id TEXT,resource_type TEXT,applications TEXT,permitted_uses TEXT)")
        self.db.execute("INSERT INTO capacity_resources VALUES('head-192-0-2-2','shared_inference','[\"capstone\"]','[\"inference\"]')")
        self.db.commit()

    def authenticate(self, app, token):
        if app != "capstone" or token != "fixture-token":
            raise PermissionError("invalid fixture identity")
        return {"models": json.dumps(["qwen3.5:4b", "all-minilm:latest"])}

    def client_limits(self, app):
        return {"operations": ["/api/generate", "/api/chat", "/api/embed"], "max_context": None}

    @contextmanager
    def connect(self):
        with self.db:
            yield self.db


def _real_gateway_server():
    if not (GATEWAY_SOURCE / "inference_gateway" / "capacity_gateway_server.py").is_file():
        pytest.skip("The released Inference Gateway 0.3.0 source is not available beside Capstone")
    sys.path.insert(0, str(GATEWAY_SOURCE))
    import inference_gateway
    if not str(getattr(inference_gateway, '__version__', '0.3.0')).startswith(('0.3.', '0.4.0', '0.4.1', '0.4.2', '0.4.3', '0.4.4', '0.4.5')):
        # This test drives the borrowed Gateway.availability with a 0.3.0-shaped
        # fake; Gateway 0.4.6's availability needs broker internals the fake
        # does not model. Skipped with the reason, never counted as a pass.
        pytest.skip(f"Inference Gateway source beside Capstone is {inference_gateway.__version__}; this fake models the 0.3.0 handler")
    from inference_gateway.capacity_gateway import Gateway
    from inference_gateway.capacity_gateway_server import Server

    class ContractGateway:
        availability = Gateway.availability

        def __init__(self):
            self.broker = _Broker()
            self.calls = []

        def models(self, app, token):
            self.broker.authenticate(app, token)
            details = {
                "qwen3.5:4b": {
                    "qualified_operations": ["completion"],
                    "qualified_capabilities": ["completion"],
                    "execution_policy": {"max_context": 8192},
                },
                "all-minilm:latest": {
                    "qualified_operations": ["embedding"],
                    "qualified_capabilities": ["embedding"],
                    "execution_policy": {},
                },
            }
            return {"contract": "shared.inference/1", "resources": [{
                "resource_id": "head-192-0-2-2",
                "models": list(details),
                "model_details": details,
            }]}

        def policy(self, app, token):
            self.broker.authenticate(app, token)
            return {"contract": "shared.inference/1", "application_id": app, "policy": {"enabled": True}}

        def _reason(self, db, row, model, purpose, num_ctx, capabilities, now):
            self.calls.append({"model": model, "purpose": purpose, "capabilities": list(capabilities)})
            return "available"

    gateway = ContractGateway()
    server = Server(("127.0.0.1", 0), gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return gateway, server, thread


def test_health_uses_real_gateway_030_handler_and_operation_capability_boundary(monkeypatch, tmp_path):
    gateway, server, thread = _real_gateway_server()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", "capstone", "fixture-token")
        with pytest.raises(OSError, match="HTTP Error 400: Bad Request"):
            client.availability("qwen3.5:4b", "/api/generate", capabilities=["completion"])

        monkeypatch.setenv("CAPSTONE_INFERENCE_MODE", "gateway")
        monkeypatch.setattr(gateway_transport, "client", lambda workload="operational": client)
        registry = Registry(tmp_path / "ai_configuration.db")
        health = registry.health("gateway")
        checked = registry.validate_available(registry.get("profiles", "gateway-default"))

        assert health["reachable"] is True
        assert checked["passed"] is True, checked["errors"]
        assert {row["name"] for row in health["installed_models"]} == {"qwen3.5:4b", "all-minilm:latest"}
        assert {call["purpose"] for call in gateway.calls} == {"completion", "embedding"}
        assert all(call["capabilities"] == [] for call in gateway.calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_one_model_availability_error_does_not_collapse_gateway_endpoint(monkeypatch, tmp_path):
    class PartiallyFailingClient:
        def models(self):
            details = {
                "qwen3.5:4b": {"qualified_operations": ["completion"], "qualified_capabilities": ["completion"], "execution_policy": {"max_context": 8192}},
                "broken:1": {"qualified_operations": ["completion"], "qualified_capabilities": ["chat"], "execution_policy": {"max_context": 8192}},
                "all-minilm:latest": {"qualified_operations": ["embedding"], "qualified_capabilities": ["embedding"], "execution_policy": {}},
            }
            return {"resources": [{"resource_id": "head-192-0-2-2", "models": list(details), "model_details": details}]}

        def policy(self):
            return {"application_id": "capstone"}

        def availability(self, model, operation, **kwargs):
            if model == "broken:1":
                raise OSError('Gateway HTTP 400: model-specific fixture rejection')
            return {"status": "available"}

    monkeypatch.setattr(gateway_transport, "client", lambda workload="operational": PartiallyFailingClient())
    registry = Registry(tmp_path / "settings.db")
    health = registry.health("gateway")
    models = {row["name"]: row for row in health["installed_models"]}
    assert health["reachable"] is True
    assert models["qwen3.5:4b"]["currently_available"] is True
    assert models["all-minilm:latest"]["currently_available"] is True
    assert "model-specific fixture rejection" in models["broken:1"]["last_error"]
    assert "model-specific fixture rejection" in health["last_error"]


def test_rc6_configuration_migrates_to_available_gateway_without_losing_experiment_history(monkeypatch, tmp_path):
    path = tmp_path / "ai_configuration.db"
    routes = {role: {"endpoint": "local", "model": "qwen3.5:4b", "enabled": True, "options": {"temperature": 0.1, "num_ctx": 8192}} for role in ROLES}
    legacy = {"id": "baseline-local", "name": "Baseline · local Qwen3.5 4B", "default_endpoint": "local", "routes": routes, "retrieval_profile": "minilm", "strict": True, "case_budget_seconds": 300}
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE endpoints(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE profiles(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE experiments(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE experiment_runs(id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL,payload TEXT NOT NULL);""")
        db.execute("INSERT INTO endpoints VALUES('local',?)", (canonical({"id": "local", "name": "Development PC", "url": "http://127.0.0.1:11434"}),))
        db.execute("INSERT INTO endpoints VALUES('gpu',?)", (canonical({"id": "gpu", "name": "GPU workstation", "url": "http://192.0.2.2:11434"}),))
        db.execute("INSERT INTO profiles VALUES('baseline-local',?)", (canonical(legacy),))
        db.execute("INSERT INTO preferences VALUES('active_profile','baseline-local')")
        db.execute("INSERT INTO experiments VALUES('EXP-OLD',?)", (canonical({"id": "EXP-OLD", "status": "completed"}),))
        db.execute("INSERT INTO experiment_runs VALUES('RUN-OLD','EXP-OLD',?)", (canonical({"id": "RUN-OLD", "experiment_id": "EXP-OLD", "status": "completed"}),))

    registry = Registry(path)
    assert [row["id"] for row in registry.list("endpoints")] == ["gateway"]
    assert registry.get("profiles", "baseline-local") is None
    assert registry.get("profiles", "baseline-gpu") is None
    active = registry.active()
    assert active["id"] == "gateway-default" and active["name"] == "Gateway · Qwen3.5 4B"
    assert {route["endpoint"] for route in active["routes"].values()} == {"gateway"}
    assert {route["model"] for route in active["routes"].values()} == {"qwen3.5:4b"}
    assert {route["options"]["num_ctx"] for route in active["routes"].values()} == {8192}
    assert registry.get("experiments", "EXP-OLD")["status"] == "completed"
    assert registry.get("experiment_runs", "RUN-OLD")["status"] == "completed"

    class AvailableGateway:
        def models(self):
            return {"resources": [{"resource_id": "head-192-0-2-2", "models": ["qwen3.5:4b"], "model_details": {
                "qwen3.5:4b": {"qualified_operations": ["completion"], "qualified_capabilities": ["completion"], "execution_policy": {"max_context": 8192}}
            }}]}

        def policy(self):
            return {"application_id": "capstone", "policy": {"enabled": True}}

        def availability(self, model, operation, **kwargs):
            return {"status": "available", "resources": {"head-192-0-2-2": "available"}}

    monkeypatch.setenv("CAPSTONE_INFERENCE_MODE", "gateway")
    monkeypatch.setattr(gateway_transport, "client", lambda workload="operational": AvailableGateway())
    assert registry.validate_available(active)["passed"] is True


def test_research_smoke_snapshot_reaches_pinned_gateway_head_and_arena(monkeypatch, tmp_path):
    head = "head-192-0-2-2"

    class ResearchClient:
        def availability(self, model, operation, **kwargs):
            return {"status": "available", "resources": {head: "available"}}

        def infer(self, model, nodes, operation, payload, **kwargs):
            assert nodes == [head]
            assert operation == "/api/generate"
            return {"status": "completed", "result": {
                "response": {"model": model, "done": True, "response": "fixture response", "eval_count": 2},
                "resource_id": head, "model": model, "model_digest": "sha256:fixture",
                "request_id": "REQ-RC7", "provenance": {"request_id": "REQ-RC7", "resource_id": head},
            }}

    monkeypatch.setenv("CAPSTONE_INFERENCE_MODE", "gateway")
    monkeypatch.setenv("CAPSTONE_GATEWAY_CLIENT_CONFIG", str(tmp_path / "operational.json"))
    monkeypatch.setenv("CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG", str(tmp_path / "research.json"))
    monkeypatch.setattr(gateway_transport, "client", lambda workload="operational": ResearchClient())
    registry = Registry(tmp_path / "ai_configuration.db")
    planned = experiments.preview(registry, {
        "profile_ids": ["gateway-default"], "gateway_models": ["qwen3.5:4b"],
        "gateway_head_ids": [head], "scenario_ids": ["qms-technical"],
        "repetitions": 1, "approval_mode": "scripted", "level": "system",
    })
    snapshot = planned["profiles"][0]
    for route in snapshot["routes"].values():
        route["cluster_nodes"] = [head]

    store = Store(str(tmp_path / "operations.db"))
    store.routing_enabled = True
    store.inference_registry = registry
    try:
        with inference.session(store, "RUN-RC7", case_id="CASE-RC7", snapshot=snapshot):
            with inference.specialist("ticket_interpretation"):
                result = inference.infer("/api/generate", {"prompt": "synthetic smoke", "options": {"num_predict": 8}}, 10)
        assert result["response"] == "fixture response"
        started = store.one("SELECT * FROM arena_events WHERE workflow_id='RUN-RC7' AND event_type='inference.started'")
        assert started["inference_node"] == head
    finally:
        store.close()


def test_lab_heads_script_prints_each_failed_run_error(tmp_path):
    heads = ["head-192-0-2-2", "head-192-0-2-3"]
    started = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/lab/state"):
                return self.reply({"inventory_status":"ready","heads": [{"resource_id": head, "serving": True, "models": [{"name": "qwen3.5:4b", "kind": "chat", "usable": True, "size_bytes": 1}]} for head in heads]})
            if self.path.startswith("/api/lab/results"):
                return self.reply({"experiments": [{"id": eid, "runs": [{"status": "failed", "error": f"failure from {head}", "arena_rows_written": 0}]} for eid, head in started]})
            self.reply({"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            eid = f"EXP-{len(started) + 1}"
            started.append((eid, body["heads"][0]))
            self.reply({"id": eid})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data_root = tmp_path / "data-root"
        (data_root / "config").mkdir(parents=True)
        token = data_root / "developer.token"
        token.write_text("fixture-token", encoding="utf-8")
        (data_root / "config" / "appsettings.json").write_text(json.dumps({
            "listen_host": "127.0.0.1", "listen_port": server.server_port, "developer_token_file": str(token)
        }), encoding="utf-8")
        completed = subprocess.run([
            "powershell.exe", "-NoProfile", "-File", str(ROOT / "deploy" / "windows" / "Test-CapstoneLabHeads.ps1"),
            "-DataRoot", str(data_root), "-TimeoutSeconds", "10"
        ], capture_output=True, text=True, timeout=30)
        lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip().startswith("{")]
        assert completed.returncode == 1
        assert len(lines) == 3
        assert [line["resource_id"] for line in lines[:2]] == heads
        assert [line["error"] for line in lines[:2]] == [f"failure from {head}" for head in heads]
        assert lines[-1] == {"passed": False, "targets": 2}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_startup_text_is_gateway_only():
    text = (ROOT / "run_app.py").read_text(encoding="utf-8")
    assert "authenticated Inference Gateway routing enabled" in text
    assert "pinned private Ollama routing enabled" not in text
    assert "configure AI Configuration" not in text
    assert "--no-llm" not in text
