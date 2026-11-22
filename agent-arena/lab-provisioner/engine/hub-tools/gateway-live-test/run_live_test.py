"""Acceptance row 20 of Inference Gateway 0.4.0 (docs/ACCEPTANCE_PLAN.md, open item O8), run from the hub.

Runs the Gateway project's own tests/test_live_ollama.py (copied here unchanged; its SHA-256 is checked) against ONE
idle lab head, with the Gateway's own code from the 0.4.0 release on this stick. The hub has no pytest, so this runner
supplies the three things the test file uses (pytest.mark.skipif, pytest.raises, tmp_path). Nothing on the hub or the
head is changed: the test only sends Ollama a few short requests and reads /api/ps.

    python -I run_live_test.py --head 192.0.2.5 --package <stick>\\AGENT-ARENA-LAB-x\\packages\\gateway\\inference-gateway-0.4.0.zip
           [--model qwen3:4b] --out <stick>\\reports\\<computer>
Exit 0: every check passed; 1: a check failed or the head was not idle; 2: bad input.
"""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import types
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST = HERE / "test_live_ollama.py"
TEST_SHA256 = "be5fca48979729ae53a33c5e873a1e792ef89fabec1cf10408fcc136b48f2acc"   # the Gateway project's tests/test_live_ollama.py, copied unchanged on 2026-11-12


def pytest_shim():
    """The parts of pytest the Gateway's test file uses."""
    m = types.ModuleType("pytest")

    class Mark:
        @staticmethod
        def skipif(condition, reason=""):
            return ("skipif", condition, reason)

    @contextlib.contextmanager
    def raises(kind, match=None):
        try:
            yield
        except kind as exc:
            if match and not re.search(match, str(exc)):
                raise AssertionError("raised %s, but %r does not match %r" % (kind.__name__, str(exc), match))
            return
        raise AssertionError("did not raise " + kind.__name__)

    m.mark = Mark()
    m.raises = raises
    return m


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--head", required=True)
    p.add_argument("--package", required=True)
    p.add_argument("--model", default="qwen3:4b")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if not re.fullmatch(r"192\.0\.2\.(\d{1,3})", a.head):
        print("--head must be a lab head address"); return 2
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    report = {"schema": "agent-arena.gateway-live-test/1", "acceptance_row": 20, "head": a.head, "model": a.model,
              "started": datetime.datetime.now().isoformat(timespec="seconds"), "checks": [], "ok": False}
    lines = []

    def say(t):
        print("  " + t, flush=True); lines.append(t)

    def finish(code):
        report["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        (out / ("gateway-live-test-%s.json" % stamp)).write_text(json.dumps(report, indent=2), encoding="utf-8")
        (out / ("gateway-live-test-%s.txt" % stamp)).write_text("\n".join(lines) + "\n", encoding="utf-8")
        say("report: " + str(out / ("gateway-live-test-%s.txt" % stamp)))
        return code

    digest = hashlib.sha256(TEST.read_bytes()).hexdigest()
    report["test_file_sha256"] = digest
    if TEST_SHA256 != "REPLACED_AT_BUILD" and digest != TEST_SHA256:
        say("the test file is not the Gateway project's (hash differs): nothing was run"); return finish(2)
    origin = "http://%s:11434" % a.head
    # The head must be idle: the second check expects only its own model loaded.
    try:
        version = get(origin + "/api/version")["version"]; loaded = [m.get("name") for m in get(origin + "/api/ps").get("models", [])]
        tags = [m.get("name") for m in get(origin + "/api/tags").get("models", [])]
    except Exception as exc:
        say("head %s does not answer Ollama from here: %s" % (a.head, exc)); return finish(1)
    report.update(ollama_version=version, loaded_before=loaded)
    say("head %s: Ollama %s; loaded now: %s" % (a.head, version, ", ".join(loaded) or "nothing"))
    if loaded:
        say("NOT IDLE: wait until nothing is loaded (about 2 minutes after its last request) and run this again"); return finish(1)
    if a.model not in tags:
        say("the head has no %s: choose another head" % a.model); return finish(1)
    # The Gateway's own code, from the release on this stick (the wheel inside the release zip).
    work = Path(tempfile.mkdtemp(prefix="gw-live-"))
    with zipfile.ZipFile(a.package) as z:
        wheel = next((n for n in z.namelist() if n.endswith(".whl")), None)
        if not wheel:
            say("no wheel in " + a.package); return finish(2)
        z.extract(wheel, work)
    with zipfile.ZipFile(work / wheel) as w:
        w.extractall(work / "site")
    sys.path.insert(0, str(work / "site"))
    sys.modules["pytest"] = pytest_shim()
    os.environ["INFERENCE_GATEWAY_LIVE_OLLAMA"] = origin
    os.environ["INFERENCE_GATEWAY_LIVE_MODEL"] = a.model
    spec = importlib.util.spec_from_file_location("test_live_ollama", TEST)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    import inference_gateway
    report["gateway_code"] = getattr(inference_gateway, "__version__", "?")
    ok = True
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        buf = io.StringIO(); t0 = time.time()
        try:
            with contextlib.redirect_stdout(buf):
                getattr(mod, name)(Path(tempfile.mkdtemp(prefix="gw-live-t-")))
            result, detail = "PASS", ""
        except Exception:
            result, detail, ok = "FAIL", traceback.format_exc(limit=4), False
        report["checks"].append({"test": name, "result": result, "seconds": round(time.time() - t0, 1), "output": buf.getvalue(), "detail": detail})
        say("%s  %s (%.0f s)" % (result, name, time.time() - t0))
        if detail:
            lines.append(detail)
    report["ok"] = ok
    say("RESULT " + ("success: Ollama %s on %s answers as the Gateway's design note says" % (version, a.head) if ok else "failed: see the lines above"))
    return finish(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
