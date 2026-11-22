import ast
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from capstone_rag import config, experiments, generation
from capstone_rag.ai_settings import Registry, fingerprint
from capstone_rag.store import Store


ROOT = Path(__file__).resolve().parents[1]
HEAD = "head-192-0-2-3"


def stand_in_reviewer(view):
    """TEST DOUBLE for the reviewer model (mechanics only, not evidence).

    Decides from the reviewer's observation alone - it never sees scenario
    facts - roughly as a careful representative would.
    """
    point = view.get("decision_point");case = view.get("case") or {}
    evidence = view.get("evidence") or []
    if view.get("review_mode") == "inspect_section":
        text = " ".join(e["content"] for e in evidence if e["kind"] == "draft_part").lower()
        notes = ([{"kind": "contradiction", "text": "Section tells the customer to open the unit.", "section": view.get("section_under_review", ""), "source_ids": []}]
                 if "open the rear panel" in text else [])
        return {"decision": "record_findings", "notes": notes}
    conversation = [e["content"] for e in evidence if e["kind"] == "conversation"]       # newest first
    findings = [e["content"] for e in evidence if e["kind"] == "agent_finding"]
    view = {**view, "conversation": list(reversed(conversation)), "agent_findings": findings}
    if view.get("withheld") and point == "draft" and any(w["id"].startswith("draft:part") for w in view["withheld"]):
        return {"decision": "request_evidence", "rationale": "Part of the draft is not shown.", "revision_instructions": "", "override_rationale": "",
                "determination": "n/a", "escalate_to": "n/a", "requested_evidence": [w["id"] for w in view["withheld"] if w["id"].startswith("draft:part")],
                "notes": [], "synthesis": "", "resolved_note_ids": [], "objections": []}
    last_in = next((m["body"] for m in reversed(view.get("conversation") or []) if m["direction"] == "inbound"), "") or ""
    open_items = (view.get("prior_review_notes") or {}).get("open_item_ids") or []
    d = {"decision": "defer", "rationale": "Waiting for more information.", "revision_instructions": "", "override_rationale": "",
         "determination": "n/a", "escalate_to": "n/a", "requested_evidence": [], "notes": [], "synthesis": "Whole artifact reviewed.",
         "resolved_note_ids": [], "objections": []}
    if open_items and point == "draft":
        d.update(decision="request_revision", rationale="Earlier review notes record unresolved problems.",
                 revision_instructions="Fix the contradictions recorded in the review notes.")
        return d
    if point == "draft":
        draft = view.get("draft") or {}
        if view.get("previous_attempt"):
            d.update(decision="request_revision", rationale="Sending was refused by the application.",
                     revision_instructions=("Ask the customer to confirm their serial number and name; do not give device-specific "
                                            "instructions until the account is verified." if not case.get("identity_verified") else
                                            "Remove anything the application flagged and keep only supported guidance."))
        elif draft.get("validation_disposition") in ("pass", "not validated"):
            d.update(decision="approve_send", rationale="Validated, grounded draft.",
                     override_rationale="Deterministic checks passed; model concern reviewed." if draft.get("independent_review_passed") is False else "")
        else:
            d.update(decision="request_revision", rationale="Validation found issues.", revision_instructions="Fix the validation issues; keep it short.")
    elif point == "movement":
        proposal = view.get("proposal") or {}
        ok = case.get("identity_verified") and proposal.get("movement_type") == "part_outbound"
        d.update(decision="approve" if ok else "reject", rationale="Verified customer, identified part." if ok else "Not warranted or identity not verified.")
    elif point == "movement_failed":
        d.update(decision="retry", rationale="Transient failure.")
    elif point == "qa":
        text = " ".join(m["body"] for m in view.get("conversation") or [] if m["direction"] == "inbound").lower()
        determination = ("suspected adverse event" if any(t in text for t in ("blister", "doctor", "needed to see")) else
                         "device hazard without confirmed injury" if any(t in text for t in ("smell", "smoke", "hazard")) else "insufficient information")
        d.update(decision="determine", determination=determination, rationale="Determination from the reported facts.")
    elif point == "identity":
        d.update(decision="decline_identity", rationale="Two matching details not provided.")
    elif point == "revision":
        d.update(decision="defer", rationale="Revision evidence insufficient.")
    elif point == "disposition":
        if case.get("safety_hold"):
            d.update(decision="escalate", escalate_to="qa_team", rationale="Safety hold requires QA follow-up beyond my authority.")
        elif any(t in last_in.lower() for t in ("resolves it", "powers on now", "that fixed", "working now")):
            d.update(decision="resolve", rationale="Customer confirmed the problem is solved.")
        elif "rather not" in last_in.lower() or "do not want to troubleshoot" in last_in.lower():
            d.update(decision="close_unresolved", rationale="Customer will not progress the case.")
        elif "conflict" in json.dumps(view.get("agent_findings")).lower() or "document" in json.dumps(view.get("agent_findings")).lower():
            d.update(decision="escalate", escalate_to="document_control", rationale="Conflicting controlled documents need document control.")
        elif not any(m["direction"] == "outbound" and m["status"] == "sent" for m in view.get("conversation") or []):
            d.update(decision="close_unresolved", rationale="Nothing could be sent to the customer; the case cannot progress from here.")
    return d


def _schema_value(schema, context, name=""):
    if name == "" and "decision" in (schema.get("properties") or {}) and context.get("decision_point"):
        value = stand_in_reviewer(context)
        # Only fields the requested schema declares (rc.25 added draft objections).
        return {k: v for k, v in value.items() if k in (schema.get("properties") or {})}
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "boolean":
        return name not in {"needs_human"}
    if kind in {"integer", "number"}:
        return 0
    if kind == "array":
        return []
    if kind == "object":
        return {key: _schema_value((schema.get("properties") or {}).get(key, {}), context, key)
                for key in schema.get("required", [])}
    if name == "evidence_quote":
        body = context.get("body") or context.get("observed_body") or context.get("request") or "P4800"
        return str(body)[:40]
    if name == "intent":
        return "technical support"
    if name in {"uncertainty", "concern", "suggested_account_id"}:
        return ""
    if name == "reply":
        if isinstance(context.get("accepted_corrections"), list):
            # rc.25 explicit revision request (test double): a safe
            # clarification that keeps the task and adds no device step.
            if not context.get("identity_verified"):
                return ("Hello there,\n\nThank you for getting in touch. Could you please confirm the serial number printed on the "
                        "label of your unit and the full name on the account, so that we can find the right record?\n\nKind regards,\nCustomer Support")
            return context.get("original_draft") or "Thank you for the details."
        raw = str(context.get("__raw__") or "").lower()
        if "power" in raw and ("mains" in raw or "cord" in raw or "lead" in raw):
            return ("Thank you for the details. With the unit switched off, please check that the power cord is pushed firmly into the "
                    "inlet at the back and into a working outlet, then switch it on. Let me know what you observe.")
        return "Thank you for the details. Please keep the enclosure closed while a representative reviews the approved P4800 guidance."
    if name == "answer":
        return "Use only the supplied approved evidence and keep the enclosure closed."
    return "fixture"


_MINILM = None


def _minilm():
    global _MINILM
    if _MINILM is None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer
        _MINILM = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _MINILM


class GatewayStandIn:
    """Stand-in shared.inference/1 Gateway.

    fault: None | 'tools_400' (tool-calling requests rejected with HTTP 400, as
    for models without tool support) | 'truncate' (structured answers cut off,
    done_reason=length) | 'hang' (requests never complete: deadline/timeout)
    | 'unavailable' (availability reports the node unavailable).
    """
    def __init__(self, fail_submit=False, fault=None, real_embeddings=False, models=("qwen3.5:4b",), no_tools_models=()):
        self.fail_submit = fail_submit
        self.fault = fault
        self.models = list(models)
        self.no_tools_models = set(no_tools_models)
        # real_embeddings: answer /api/embed with the same MiniLM model the
        # index was built with, so retrieval behaves as on the heads.
        self.real_embeddings = real_embeddings
        self.submissions = []
        self.requests = {}
        # rc.25: deterministic role-output overrides keyed by the set of schema
        # property names (e.g. supervise_finding), for Phase 3 stage proofs.
        self.role_overrides = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Inference-Service-ID", "shared-inference")
                self.end_headers()
                self.wfile.write(data)

            def identity(self):
                assert self.headers.get("X-Application-ID") == "capstone-research"
                assert self.headers.get("X-Infrastructure-Token") == "fixture-research-token"

            def do_GET(self):
                self.identity()
                route = urllib.parse.urlsplit(self.path)
                if route.path == "/v1/models":
                    details = {name: {"qualified_operations": ["completion"], "qualified_capabilities": ["completion"], "execution_policy": {"max_context": 8192}}
                               for name in outer.models}
                    details["all-minilm:latest"] = {"qualified_operations": ["embedding"], "qualified_capabilities": ["embedding"], "execution_policy": {}}
                    return self.send_json(200, {"contract": "shared.inference/1", "resources": [{"resource_id": HEAD, "models": list(details), "model_details": details}]})
                if route.path == "/v1/policy":
                    return self.send_json(200, {"contract": "shared.inference/1", "application_id": "capstone-research", "policy": {"enabled": True}})
                if route.path == "/v1/availability":
                    query = urllib.parse.parse_qs(route.query)
                    capabilities = [item for item in query.get("capabilities", [""])[0].split(",") if item]
                    if set(capabilities) - {"chat", "tools", "structured_output", "vision", "context"}:
                        return self.send_json(400, {"error": "Unknown capability"})
                    if outer.fault == "unavailable":
                        return self.send_json(200, {"contract": "shared.inference/1", "status": "unavailable", "reason": "resource_unhealthy", "resources": {HEAD: "unhealthy"}})
                    return self.send_json(200, {"contract": "shared.inference/1", "status": "available", "resources": {HEAD: "available"}})
                if route.path.startswith("/v1/requests/"):
                    rid = route.path.rsplit("/", 1)[-1]
                    return self.send_json(200, outer.requests[rid])
                return self.send_json(404, {"error": "not found"})

            def do_POST(self):
                self.identity()
                if self.path != "/v1/submit":
                    return self.send_json(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                outer.submissions.append(body)
                if outer.fail_submit:
                    return self.send_json(503, {"error": "fixture Gateway unavailable"})
                # Model calls are pinned to the head; query embeddings are not
                # pinned (the Gateway chooses), as in production.
                assert body["nodes"] == [HEAD] or (body["operation"] == "/api/embed" and body["nodes"] == ["@authorized"]), body["nodes"]
                request_id = "REQ-" + uuid.uuid4().hex
                operation = body["operation"]
                payload = body["payload"]
                if payload.get("tools") and body["model"] in outer.no_tools_models:
                    return self.send_json(400, {"error": f"registry.ollama.ai/library/{body['model']} does not support tools"})
                if outer.fault == "tools_400" and payload.get("tools"):
                    return self.send_json(400, {"error": "registry.ollama.ai/library/fixture does not support tools"})
                if outer.fault == "hang" and operation != "/api/embed":
                    outer.requests[request_id] = {"contract": "shared.inference/1", "status": "queued",
                        "request": {"request_id": request_id, "status": "queued", "resource_id": None}}
                    return self.send_json(202, {"contract": "shared.inference/1", "status": "queued", "request": {"request_id": request_id, "status": "queued", "resource_id": None}})
                if operation == "/api/embed":
                    values=payload.get("input") or payload.get("prompt") or [""]
                    count=len(values) if isinstance(values,list) else 1
                    if outer.real_embeddings:
                        texts = values if isinstance(values, list) else [values]
                        upstream = {"model": body["model"], "embeddings": _minilm().encode(texts, normalize_embeddings=True).tolist()}
                    else:
                        upstream = {"model": body["model"], "embeddings": [[0.01] * 384 for _ in range(count)]}
                elif operation == "/api/chat":
                    context = {}
                    messages = payload.get("messages") or []
                    if messages:
                        try:
                            candidate = json.loads(messages[-1].get("content") or "{}")
                            if isinstance(candidate, dict):
                                context = candidate
                        except (ValueError, TypeError):
                            context = {"__raw__": messages[-1].get("content") or ""}
                    if payload.get("tools") and not payload.get("format"):
                        message = {"content": "", "tool_calls": [{"function": {"name": payload["tools"][0]["function"]["name"], "arguments": {}}}]}
                    else:
                        fmt = payload.get("format") or {}
                        override = outer.role_overrides.get(frozenset((fmt.get("properties") or {}).keys())) if fmt else None
                        value = (dict(override) if override is not None else
                                 _schema_value(fmt or {"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}}, context))
                        message = {"content": json.dumps(value)}
                    done_reason = "stop"
                    if outer.fault == "truncate" and payload.get("format"):
                        message = {"content": message["content"][:max(1, len(message["content"]) // 2)]}
                        done_reason = "length"
                    upstream = {"model": body["model"], "message": message, "done": True, "done_reason": done_reason, "eval_count": 8, "prompt_eval_count": 12}
                else:
                    upstream = {"model": body["model"], "response": "fixture response", "done": True, "eval_count": 8, "prompt_eval_count": 12}
                # fault 'substitute': the serving side reports a different model
                # than requested (routing verification must catch it).
                executed = "substitute:1b" if outer.fault == "substitute" and operation != "/api/embed" else body["model"]
                completed = {"contract": "shared.inference/1", "status": "completed",
                    "request": {"request_id": request_id, "status": "completed", "resource_id": HEAD, "lease_id": "LEASE-fixture", "nodes": json.dumps([HEAD])},
                    "execution_started_at": 1.0,
                    "result": {"response": upstream, "resource_id": HEAD, "model": executed, "model_digest": "sha256:fixture", "request_id": request_id,
                        "queued_at": 10.0, "started_at": 11.0, "finished_at": 13.0,
                        "queue_ms": 1.0, "duration_ms": 2.0, "provenance": {"request_id": request_id, "resource_id": HEAD, "executed_model": executed, "executed_digest": "sha256:fixture"}}}
                outer.requests[request_id] = completed
                self.send_json(202, {"contract": "shared.inference/1", "status": "queued", "request": {"request_id": request_id, "status": "queued", "resource_id": None}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def _copy_database(source, target):
    with sqlite3.connect(source) as before, sqlite3.connect(target) as after:
        before.backup(after)


def _worker_run(monkeypatch, tmp_path, gateway, *, architecture=None, approval_mode="scripted", scenario=None,
                level="system", specialist="system", profile_hook=None):
    data_root = tmp_path / "data"
    data_root.mkdir()
    for name in ("qms", "erp"):
        source = Path(config.DATA_DIR) / name
        if source.exists():
            shutil.copytree(source, data_root / name)
    compatibility = Path(config.COMPATIBILITY_PATH)
    if compatibility.exists():
        shutil.copy2(compatibility, data_root / compatibility.name)
    (data_root / "index").mkdir()
    shutil.copy2(config.INDEX_PATH, data_root / "index" / "qms_index.npz")

    token = tmp_path / "research.token"
    token.write_text("fixture-research-token", encoding="utf-8")
    client_config = tmp_path / "research-client.json"
    client_config.write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "service_id": "shared-inference",
        "url": gateway.url, "application_id": "capstone-research", "token_file": str(token)}), encoding="utf-8")
    monkeypatch.setenv("CAPSTONE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("CAPSTONE_INFERENCE_MODE", "gateway")
    monkeypatch.setenv("CAPSTONE_GATEWAY_CLIENT_CONFIG", str(client_config))
    monkeypatch.setenv("CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG", str(client_config))
    monkeypatch.setattr(config, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "INDEX_PATH", str(data_root / "index" / "qms_index.npz"))

    source_db = tmp_path / "live.db"
    _copy_database(config.DB_PATH, source_db)
    live = Store(str(source_db))
    registry = Registry(tmp_path / "ai_configuration.db")
    profile = registry.snapshot(registry.get("profiles", "gateway-default"))
    profile.update(gateway_workload="research", execution_mode="distributed")
    if gateway.fail_submit or getattr(gateway, "fault", None) == "hang":
        # Gateway client 0.4.1 retries a transient 503 using the same client key
        # until the request deadline. Keep this regression bounded while still
        # exercising that released behavior.
        profile["case_budget_seconds"] = 5
    for route in profile["routes"].values():
        route["cluster_nodes"] = [HEAD]
    if profile_hook:
        profile_hook(profile)
    profile["configuration_hash"] = fingerprint({key: value for key, value in profile.items() if key != "configuration_hash"})

    run_dir = data_root / "experiments" / "EXP-RC8" / "RUN-RC8"
    run_dir.mkdir(parents=True)
    snapshot = run_dir.parent / "source-snapshot.db"
    _copy_database(source_db, snapshot)
    run = {"id": "RUN-RC8", "experiment_id": "EXP-RC8", "directory": str(run_dir), "profile": profile,
        "scenario": scenario or experiments.SMOKE["qms-technical"], "iteration": 1, "seed": 7, "level": level, "specialist": specialist,
        "approval_mode": approval_mode, "criteria": list(experiments.CRITERIA), "status": "queued", "source_revision": experiments.source_revision(),
        "source_snapshot": str(snapshot), "source_snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "source_database": str(source_db), "registry_path": registry.path, "timeout_seconds": 20 if gateway.fail_submit else 45 if getattr(gateway, "fault", None) == "hang" else 120,
        "campaign_concurrency": 1, "execution_strategy": "sequential", "architecture": architecture, "curator_retrieval": None,
        "gateway_head_id": HEAD, "category": None}
    (run_dir / "manifest.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    with registry.connect() as db:
        db.execute("INSERT INTO experiments VALUES (?,?)", ("EXP-RC8", json.dumps({"id": "EXP-RC8", "source_database": str(source_db), "status": "running"})))
        db.execute("INSERT INTO experiment_runs VALUES (?,?,?)", (run["id"], run["experiment_id"], json.dumps(run)))
    result = experiments.Manager(registry).execute(run)
    return result, live, run_dir


def test_real_worker_subprocess_completes_pinned_research_smoke(monkeypatch, tmp_path):
    gateway = GatewayStandIn()
    try:
        result, live, run_dir = _worker_run(monkeypatch, tmp_path, gateway)
        try:
            assert result["status"] == "completed", (run_dir / "worker.log").read_text(encoding="utf-8")
            assert result["metrics"]["model_calls"] >= 1
            assert gateway.submissions
            assert all(body["nodes"] == [HEAD] for body in gateway.submissions)
            assert all(item["resource_id"] == HEAD for item in result["metrics"]["inference_correlations"])
            lifecycle=result["metrics"]["inference_correlations"][0]["gateway_lifecycle"]
            assert lifecycle["queued_at"] == lifecycle["submitted_at"] == 10.0
            assert lifecycle["started_at"] == lifecycle["execution_started_at"] == 11.0
            assert lifecycle["finished_at"] == lifecycle["completed_at"] == 13.0
            started = live.one("SELECT * FROM arena_events WHERE workflow_id='RUN-RC8' AND event_type='inference.started' AND inference_node=?", (HEAD,))
            assert started is not None
        finally:
            live.close()
    finally:
        gateway.close()


def test_real_worker_subprocess_gateway_503_fails_with_gateway_text(monkeypatch, tmp_path):
    gateway = GatewayStandIn(fail_submit=True)
    try:
        result, live, _ = _worker_run(monkeypatch, tmp_path, gateway)
        try:
            assert result["status"] == "failed"
            assert "Gateway HTTP 503: Service Unavailable" in result["error"]
            assert result["status"] != "paused"
        finally:
            live.close()
    finally:
        gateway.close()


def test_generation_without_pinned_session_fails_clearly_and_store_accesses_exist():
    with pytest.raises(RuntimeError, match="requires a pinned inference session"):
        generation._post("/api/chat", {}, 1)
    source = (ROOT / "src" / "capstone_rag" / "generation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "get_default" not in {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_representative_agentic_matrix_cells_execute_in_real_worker_subprocess(monkeypatch,tmp_path):
    from capstone_rag.agentic_methodologies import candidates
    selected={row['id']:row for row in candidates(['A','B','F','H'],['1','2','3','4'])}
    for index,identifier in enumerate(('method-A1','method-B2','method-F3','method-H4')):
        gateway=GatewayStandIn()
        try:
            cell=tmp_path/str(index);cell.mkdir()
            result,live,directory=_worker_run(monkeypatch,cell,gateway,
                architecture=selected[identifier],approval_mode='automated')
            try:
                assert result['status']=='completed',(directory/'worker.log').read_text(encoding='utf-8')
                assert result['metrics']['model_calls']>=1
                assert result['metrics']['communication_metrics']['topology_id']==identifier[-2]
                assert result['metrics']['communication_metrics']['treatment_id']==identifier[-1]
                assert all(item['nodes']==[HEAD] for item in gateway.submissions)
            finally:live.close()
        finally:gateway.close()


def test_all_32_agentic_matrix_cells_execute_in_real_worker_subprocess(monkeypatch,tmp_path):
    """Release acceptance: no matrix cell may depend on a component absent from its plan."""
    from capstone_rag.agentic_methodologies import candidates
    gateway=GatewayStandIn()
    completed=[]
    try:
        for index,architecture in enumerate(candidates()):
            cell=tmp_path/f"cell-{index:02d}";cell.mkdir()
            result,live,directory=_worker_run(monkeypatch,cell,gateway,
                architecture=architecture,approval_mode='automated')
            try:
                assert result['status']=='completed',f"{architecture['id']}: "+(directory/'worker.log').read_text(encoding='utf-8')
                assert result['metrics']['model_calls']>=1
                communication=result['metrics']['communication_metrics']
                assert communication['topology_id']==architecture['id'][-2]
                assert communication['treatment_id']==architecture['id'][-1]
                completed.append(architecture['id'])
            finally:live.close()
    finally:gateway.close()
    assert completed==[f'method-{letter}{number}' for letter in 'ABCDEFGH' for number in '1234']
