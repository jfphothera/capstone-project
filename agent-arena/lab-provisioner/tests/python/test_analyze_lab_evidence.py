"""The cross-machine evidence report (tools/analyze_lab_evidence.py) on a synthetic sweep: two heads and the hub."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
import analyze_lab_evidence as an  # noqa: E402


def write(folder, name, value):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value), encoding="utf-8")


def head(ip, *, tps, mode="balanced", link="1 Gbps", procs=1, tray=False, bad=()):
    procs_l = [{"pid": 100 + i, "name": "ollama.exe", "owner": "NT AUTHORITY\\SYSTEM", "path": "C:\\Program Files\\Ollama\\ollama.exe"} for i in range(procs)]
    if tray:
        procs_l.append({"pid": 900, "name": "ollama app.exe", "owner": "HEAD\\Agent", "path": "C:\\Users\\Agent\\AppData\\Local\\Programs\\Ollama\\ollama app.exe"})
    return {"schema": "agent-arena.lab-facts/1", "role": "head", "lab_address": ip, "provisioner": "1.5.9", "collected_at": "2026-11-09T15:00",
            "hardware": {"ram_gb": 31.4, "free_gb": 20.1}, "power": {"mode_on_mains": mode, "on_mains": True, "cpu_performance_pct": 120},
            "adapters": [{"name": "Ethernet", "link": link, "addresses": [f"{ip}/24 Preferred"], "rx_errors": 0, "tx_errors": 0, "power_settings": []}],
            "ollama": {"version": "0.40.13", "models": ["qwen3.5:4b"]}, "duplicates": {"ollama_processes": procs_l, "ports": {}, "ollama_installs": []},
            "expectations": [{"check": c, "ok": False, "detail": "x"} for c in bad], "ping": {"192.0.2.254": {"received": 3, "avg_ms": 0.5, "max_ms": 1}},
            "speed": {}, "neighbours": []}


def test_the_report_puts_heads_side_by_side_and_flags_what_is_wrong(tmp_path):
    r = tmp_path / "reports"
    write(r / "DESKTOP-REDACT8" / "evidence-20261109-150000", "facts.json", head("192.0.2.7", tps=11))
    write(r / "DESKTOP-REDACT8" / "evidence-20261109-150000", "ollama/speed.json", {"address": "192.0.2.7", "model": "qwen3.5:4b", "cold": {"wall_s": 70, "load_s": 60}, "warm": {"wall_s": 5, "tokens_per_s": 11.0}, "ctx2048": {"wall_s": 30}})
    write(r / "DESKTOP-REDACT13" / "evidence-20261109-151000", "facts.json", head("192.0.2.8", tps=3, mode="best power efficiency", link="100 Mbps", procs=2, tray=True, bad=["boot task runs"]))
    write(r / "DESKTOP-REDACT13" / "evidence-20261109-151000", "ollama/speed.json", {"address": "192.0.2.8", "model": "qwen3.5:4b", "cold": {"wall_s": 300, "load_s": 280}, "warm": {"wall_s": 16, "tokens_per_s": 3.0}, "ctx2048": {"wall_s": 90}})
    hub = {"schema": "agent-arena.lab-facts/1", "role": "hub", "lab_address": "192.0.2.254", "provisioner": "1.5.9",
           "gateway_heads": [{"resource_id": "head-192-0-2-7", "health": "healthy", "qualified": 20}, {"resource_id": "head-192-0-2-5", "health": "offline", "qualified": 15}],
           "neighbours": [{"ip": "192.0.2.9", "mac": "aa-aa"}], "expectations": []}
    write(r / "DESKTOP-REDACT10" / "evidence-20261109-152000", "facts.json", hub)
    write(r / "DESKTOP-REDACT10" / "evidence-20261109-152000", "head-speed.json", [{"address": "192.0.2.7", "model": "qwen3.5:4b", "cold": {"wall_s": 72}, "warm": {"tokens_per_s": 10.8}}])
    write(r / "DESKTOP-REDACT11" / "evidence-20261109-152500", "facts.json", {"role": "capstone", "lab_address": "192.0.2.201", "neighbours": [{"ip": "192.0.2.9", "mac": "bb-bb"}], "expectations": []})
    text = an.report(*an.analyse(r))
    assert "| 192.0.2.7 | DESKTOP-REDACT8 | 0.40.13 |" in text and "70 / 11.0 / 30" in text and "72 / 10.8" in text
    assert "192.0.2.5" in text and "offline" in text, "a head only the Gateway knows still gets a row"
    assert "192.0.2.8: 3.0 tok/s" in text, "slower than its peers"
    assert "2 ollama.exe processes" in text and "tray app" in text
    assert "boot task runs" in text
    assert "100 Mbps (not 1 Gbps)" in text
    assert "192.0.2.9 answers from 2 different network cards" in text, "address collision across machines"
    assert an.gbps("2.5 Gbps") == 2.5 and an.gbps("100 Mbps") == 0.1 and an.gbps("") is None
