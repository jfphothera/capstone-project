import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from capstone_rag.webapp.app import create_app
from capstone_rag.webapp.routers import lab


ROOT=Path(__file__).resolve().parents[1]


def test_capacity_pressure_plan_is_one_fixed_call_per_run(monkeypatch):
    available={"name":"qwen3.5:2b","heads":["head-192-0-2-3","head-192-0-2-8"],"blocked":{}}
    monkeypatch.setattr(lab,"state",lambda:{"models":[available]})
    report=lab.plan({"experiment":"capacity-pressure","models":["qwen3.5:2b"],
        "capacity_calls":40,"concurrency":8})
    assert report["capacity_pressure"] is True
    assert report["runs"] == 40
    assert report["parallel_flows"] == 8
    assert report["waves"] == 5
    assert report["eligible_heads_by_model"]["qwen3.5:2b"] == available["heads"]


def test_public_health_does_not_wait_for_gateway_probe(tmp_path,monkeypatch):
    from capstone_rag import generation
    from capstone_rag.store import Store
    monkeypatch.setenv("CAPSTONE_AI_SETTINGS",str(tmp_path/"settings.db"))
    def blocked():
        time.sleep(1)
        raise TimeoutError("Gateway unavailable")
    monkeypatch.setattr(generation,"model_status",blocked)
    with TestClient(create_app(prefer_llm=True,store=Store(str(tmp_path/"health.db")),
            start_workers=False)) as client:
        started=time.perf_counter();response=client.get("/api/health");elapsed=time.perf_counter()-started
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["degraded"] is True
    assert elapsed < .5


def test_gateway_only_pages_do_not_request_retired_ai_configuration():
    static=ROOT/"src"/"capstone_rag"/"webapp"/"static"/"js"
    assert "/api/ai/configuration" not in (static/"experiment_ui.js").read_text(encoding="utf-8")
    assert "/api/ai/configuration" not in (static/"copilot.js").read_text(encoding="utf-8")
    assert "/api/experiments/configuration" in (static/"experiment_ui.js").read_text(encoding="utf-8")


def test_gateway_client_is_the_unmodified_0_4_1_release():
    import hashlib
    path=ROOT/"src"/"shared_inference_client.py";raw=path.read_bytes();text=raw.decode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == "e7065e412bdf7e6777714ab51ebafa520c2984e344d964741b9f60f7a4bdce02"
    assert "__version__='0.4.1'" in text
    assert "POLL_FIRST,POLL_MAX,POLL_FACTOR=.25,2.0,1.5" in text
    assert "current=self._until('submit'" in text


def test_lab_heads_script_counts_review_pause_when_pinned_call_was_observed(tmp_path):
    head="head-192-0-2-3"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value):
            data=json.dumps(value).encode();self.send_response(200)
            self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)))
            self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path.startswith("/api/lab/state"):
                return self.reply({"inventory_status":"ready","heads":[{"resource_id":head,"serving":True,"models":[{"name":"qwen3.5:4b","kind":"chat","usable":True,"size_bytes":1}]}]})
            if self.path.startswith("/api/lab/results"):
                return self.reply({"experiments":[{"id":"EXP-PAUSED","runs":[{"status":"paused","error":None,
                    "model_calls":1,"inference_correlations":[{"resource_id":head,"gateway_request_id":"REQ-PAUSED"}],
                    "gateway_request_id":"REQ-PAUSED","duration_ms":10,"arena_rows_written":1}]}]})
            self.reply({"error":"not found"})
        def do_POST(self):
            length=int(self.headers.get("Content-Length","0"));self.rfile.read(length)
            self.reply({"id":"EXP-PAUSED"})
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        data_root=tmp_path/"data-root";(data_root/"config").mkdir(parents=True)
        token=data_root/"developer.token";token.write_text("fixture-token",encoding="utf-8")
        (data_root/"config"/"appsettings.json").write_text(json.dumps({"listen_host":"127.0.0.1",
            "listen_port":server.server_port,"developer_token_file":str(token)}),encoding="utf-8")
        completed=subprocess.run(["powershell.exe","-NoProfile","-File",str(ROOT/"deploy"/"windows"/"Test-CapstoneLabHeads.ps1"),
            "-DataRoot",str(data_root),"-TimeoutSeconds","10"],capture_output=True,text=True,timeout=30)
        lines=[json.loads(line) for line in completed.stdout.splitlines() if line.strip().startswith("{")]
        assert completed.returncode == 0, completed.stdout+completed.stderr
        assert lines[0]["status"] == "paused"
        assert lines[0]["model_calls"] == 1
        assert lines[0]["pinned_head_observed"] is True
        assert lines[-1] == {"passed":True,"targets":1}
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_unqualified_qwen3_reasoning_model_is_not_a_seed_default(tmp_path):
    from capstone_rag.ai_settings import Registry
    registry=Registry(tmp_path/"settings.db")
    assert {route["model"] for route in registry.get("profiles","gateway-default")["routes"].values()} == {"qwen3.5:4b"}
