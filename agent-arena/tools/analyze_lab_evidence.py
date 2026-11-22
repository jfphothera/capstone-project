"""One report over every lab machine's evidence (COLLECT-LAB-EVIDENCE.cmd, provisioner 1.5.9+).

    python tools/analyze_lab_evidence.py <stick>\\reports [--out report.md]

For each computer the newest evidence-* folder is read: facts.json (every machine), head-speed.json (the hub: every
head measured over the lab cable) and ollama/speed.json (a head: measured on itself). The report lists the heads side
by side, every expectation that failed, duplicates and collisions, crashes, clock drift, lost pings, and heads slower
than the others of the same hardware. It reads only; it changes nothing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def newest_evidence(reports):
    out = {}
    for comp in sorted(Path(reports).iterdir()):
        if not comp.is_dir():
            continue
        ev = sorted(comp.glob("evidence-*"))
        if ev:
            out[comp.name] = ev[-1]
    return out


def gbps(link):
    """'1 Gbps' / '100 Mbps' -> Gbps as float. Pure."""
    s = str(link or "").strip()
    try:
        v, unit = s.split()[:2]
        return float(v) / (1000 if unit.lower().startswith("m") else 1)
    except (ValueError, IndexError):
        return None


def speed_row(s):
    if not isinstance(s, dict) or s.get("error"):
        return {"error": (s or {}).get("error") if isinstance(s, dict) else "no result"}
    c, w, k = s.get("cold") or {}, s.get("warm") or {}, s.get("ctx2048") or {}
    return {"model": s.get("model"), "cold_s": c.get("wall_s"), "load_s": c.get("load_s"), "warm_s": w.get("wall_s"),
            "tps": w.get("tokens_per_s"), "ctx2048_s": k.get("wall_s"), "error": c.get("error") or w.get("error")}


def analyse(reports):
    machines = {}
    for comp, folder in newest_evidence(reports).items():
        facts = load(folder / "facts.json") or {}
        machines[comp] = {"folder": folder, "facts": facts, "role": facts.get("role", "?"),
                          "hub_speed": load(folder / "head-speed.json"), "own_speed": load(folder / "ollama" / "speed.json")}
    heads = {}
    for comp, m in machines.items():
        f = m["facts"]
        if m["role"] == "head" and f.get("lab_address"):
            heads[f["lab_address"]] = {"computer": comp, "facts": f, "own": speed_row(m["own_speed"])}
    hub = next((m for m in machines.values() if m["role"] == "hub"), None)
    if hub:
        for s in hub["hub_speed"] or []:
            heads.setdefault(s.get("address"), {"computer": "?", "facts": {}, "own": None})["hub"] = speed_row(s)
        for g in (hub["facts"].get("gateway_heads") or []):
            a = "192.0.2." + str(g.get("resource_id", "")).rsplit("-", 1)[-1]
            heads.setdefault(a, {"computer": "?", "facts": {}, "own": None})["gateway"] = g
    return machines, heads


def report(machines, heads):
    L = ["# Lab evidence report", ""]
    L.append("## Machines")
    L.append("| computer | role | lab address | provisioner | collected | expectations failed |")
    L.append("|---|---|---|---|---|---|")
    for comp, m in sorted(machines.items()):
        f = m["facts"]
        bad = [e for e in f.get("expectations", []) if not e.get("ok")]
        L.append(f"| {comp} | {m['role']} | {f.get('lab_address')} | {f.get('provisioner')} | {str(f.get('collected_at', ''))[:16]} | {len(bad)} |")
    L += ["", "## Heads side by side", "",
          "| head | computer | Ollama | models | RAM / free GB | power mode | mains | CPU perf % | lab link | Gateway | own test: cold s / warm tok/s / 2048 s | from hub: cold s / warm tok/s |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tps = {}
    for a in sorted(heads, key=lambda x: int(str(x).rsplit(".", 1)[-1]) if str(x).rsplit(".", 1)[-1].isdigit() else 999):
        h = heads[a]; f = h["facts"]; hw = f.get("hardware") or {}; pw = f.get("power") or {}
        nic = next((n for n in f.get("adapters") or [] if any(str(x).startswith(f"{a}/") for x in n.get("addresses") or [])), {})
        g = h.get("gateway") or {}
        own, hb = h.get("own") or {}, h.get("hub") or {}
        o = own.get("error") or f"{own.get('cold_s')} / {own.get('tps')} / {own.get('ctx2048_s')}" if own else "-"
        r = hb.get("error") or f"{hb.get('cold_s')} / {hb.get('tps')}" if hb else "-"
        L.append(f"| {a} | {h['computer']} | {(f.get('ollama') or {}).get('version', '-')} | {len((f.get('ollama') or {}).get('models') or [])} | "
                 f"{hw.get('ram_gb', '-')} / {hw.get('free_gb', '-')} | {pw.get('mode_on_mains', '-')} | {pw.get('on_mains', '-')} | {pw.get('cpu_performance_pct', '-')} | "
                 f"{nic.get('link', '-')} | {g.get('health', '-')} ({g.get('qualified', '-')} qualified) | {o} | {r} |")
        t = (hb or {}).get("tps") or (own or {}).get("tps")
        if t:
            tps.setdefault(round(float(hw.get("ram_gb") or 0)), []).append((a, float(t)))
    L += ["", "## Slower than heads of the same RAM (warm tokens per second below 70 % of their median)"]
    slow = []
    for ram, rows in tps.items():
        if len(rows) >= 2:
            med = statistics.median(t for _, t in rows)
            slow += [f"- {a}: {t} tok/s, the {ram} GB heads' median is {med}" for a, t in rows if t < 0.7 * med]
    L += slow or ["- none"]
    L += ["", "## Expectations that failed"]
    any_bad = False
    for comp, m in sorted(machines.items()):
        for e in (m["facts"].get("expectations") or []):
            if not e.get("ok"):
                any_bad = True
                L.append(f"- **{comp}**: {e.get('check')}: {e.get('detail')}")
    if not any_bad:
        L.append("- none")
    L += ["", "## Duplicates and collisions"]
    found = []
    for comp, m in sorted(machines.items()):
        f = m["facts"]; d = f.get("duplicates") or {}
        procs = d.get("ollama_processes") or []
        serve = [p for p in procs if str(p.get("name", "")).lower() == "ollama.exe"]
        tray = [p for p in procs if "app" in str(p.get("name", "")).lower()]
        if len(serve) > 1:
            found.append(f"- **{comp}**: {len(serve)} ollama.exe processes: " + "; ".join(f"{p.get('pid')} {p.get('owner')} {p.get('path')}" for p in serve))
        if tray:
            found.append(f"- **{comp}**: Ollama tray app running ({', '.join(str(p.get('owner')) for p in tray)}): it starts its own server")
        if len(d.get("ollama_installs") or []) > 1:
            found.append(f"- **{comp}**: Ollama installed {len(d['ollama_installs'])} times: {'; '.join(d['ollama_installs'])}")
        for port, users in (d.get("ports") or {}).items():
            progs = {u.get("program") for u in users}
            if len(progs) > 1:
                found.append(f"- **{comp}**: port {port} used by {len(progs)} programs: {'; '.join(str(p) for p in progs)}")
        if (d.get("run_keys") or []) or (d.get("startup_links") or []):
            found.append(f"- **{comp}**: started at sign-in as well: {'; '.join((d.get('run_keys') or []) + (d.get('startup_links') or []))}")
        rules = d.get("inbound_rules_on_lab_ports") or []
        by_port = {}
        for r in rules:
            by_port.setdefault(r.get("port"), []).append(r.get("name"))
        for port, names in by_port.items():
            if len(names) > 1:
                found.append(f"- **{comp}**: {len(names)} inbound rules on port {port}: {'; '.join(names)}")
        if f.get("duplicate_blobs"):
            found.append(f"- **{comp}**: {f['duplicate_blobs']} model file(s) stored in more than one folder")
        if f.get("duplicate_address"):
            found.append(f"- **{comp}**: Windows reports a DUPLICATE address: {f['duplicate_address']}")
    macs = {}
    for comp, m in machines.items():
        for n in m["facts"].get("neighbours") or []:
            macs.setdefault(n.get("ip"), set()).add(str(n.get("mac")).lower())
    for ip, ms in macs.items():
        if len({x for x in ms if x and x != "00-00-00-00-00-00"}) > 1:
            found.append(f"- **address collision**: {ip} answers from {len(ms)} different network cards: {', '.join(sorted(ms))}")
    L += found or ["- none"]
    L += ["", "## Network: links, errors, lost pings, clock"]
    net = []
    for comp, m in sorted(machines.items()):
        f = m["facts"]
        for n in f.get("adapters") or []:
            if any(str(x).startswith("192.0.2.") for x in n.get("addresses") or []):
                g = gbps(n.get("link"))
                if g is not None and g < 1:
                    net.append(f"- **{comp}**: lab adapter {n.get('name')} at {n.get('link')} (not 1 Gbps)")
                errs = (n.get("rx_errors") or 0) + (n.get("tx_errors") or 0)
                if errs:
                    net.append(f"- **{comp}**: lab adapter {n.get('name')}: {errs} packet errors")
                if any("=" in str(p) and str(p).split("=", 1)[1].lower() in ("enabled", "on") for p in n.get("power_settings") or []):
                    net.append(f"- **{comp}**: lab adapter power saving on: {'; '.join(n['power_settings'])}")
        for ip, p in (f.get("ping") or {}).items():
            # Heads do not answer pings from other heads (their firewall admits the hub only): only the hub counts.
            if m["role"] == "head" and not str(ip).endswith(".254"):
                continue
            if isinstance(p, dict) and p.get("received", 0) < 3:
                net.append(f"- **{comp}**: {ip} answered {p.get('received', 0)} of 3 pings")
            elif isinstance(p, dict) and (p.get("max_ms") or 0) > 20:
                net.append(f"- **{comp}**: {ip} ping up to {p.get('max_ms')} ms on the lab cable")
        off = f.get("clock_offset_s")
        if isinstance(off, (int, float)) and abs(off) > 2:
            net.append(f"- **{comp}**: clock {off:+.1f} s from the hub")
    L += net or ["- nothing abnormal"]
    L += ["", "## Memory: committed against the limit, and what holds it"]
    mem = []
    for comp, m in sorted(machines.items()):
        mm = m["facts"].get("memory") or {}
        if not mm:
            continue
        used, lim = mm.get("committed_gb"), mm.get("commit_limit_gb")
        pct = round(100 * used / lim) if used and lim else None
        top = ", ".join(f"{p.get('name')} {p.get('committed_mb')} MB ({p.get('count')})" for p in (mm.get("by_program") or [])[:4])
        flag = " **near the limit**" if pct and pct >= 90 else ""
        mem.append(f"- **{comp}**: committed {used} of {lim} GB ({pct} %){flag}; available {mm.get('available_mb')} MB; "
                   f"pools paged {mm.get('pool_paged_mb')} / nonpaged {mm.get('pool_nonpaged_mb')} MB; most: {top}")
    L += mem or ["- not in this evidence (collector before 1.6.0)"]
    L += ["", "## Network adapters Windows has (also disabled or hidden)"]
    nets = []
    for comp, m in sorted(machines.items()):
        for n in m["facts"].get("adapters_all") or []:
            if str(n.get("status")) not in ("Up",) and "wi-fi" not in str(n.get("name", "")).lower() and "bluetooth" not in str(n.get("description", "")).lower():
                nets.append(f"- **{comp}**: {n.get('name')} ({n.get('description')}): {n.get('status')}, admin {n.get('admin')}, hidden {n.get('hidden')}")
        for d in m["facts"].get("network_devices") or []:
            if str(d.get("status")) not in ("OK", "Unknown") or (d.get("problem") not in (None, "", "0", "CM_PROB_NONE")):
                nets.append(f"- **{comp}**: device {d.get('name')}: {d.get('status')}, problem {d.get('problem')}, present {d.get('present')}")
    L += nets or ["- nothing abnormal"]
    L += ["", "## Crashes and unclean restarts (14 days)"]
    cr = []
    for comp, m in sorted(machines.items()):
        c = m["facts"].get("crashes") or {}
        for k in ("unexpected_restarts", "app_crashes", "wer_reports"):
            for x in c.get(k) or []:
                cr.append(f"- **{comp}** ({k.replace('_', ' ')}): {x}")
    L += cr or ["- none"]
    L += ["", "## Disks, Defender, pending reboots"]
    misc = []
    for comp, m in sorted(machines.items()):
        f = m["facts"]
        for d in f.get("disks") or []:
            if d.get("size_gb") and d.get("free_gb") is not None and d["free_gb"] < max(15, 0.1 * d["size_gb"]):
                misc.append(f"- **{comp}**: {d['drive']} only {d['free_gb']} GB free of {d['size_gb']} GB")
        for d in f.get("physical_disks") or []:
            if str(d.get("health")) not in ("Healthy", ""):
                misc.append(f"- **{comp}**: disk {d.get('name')} health {d.get('health')}")
        if f.get("reboot_pending"):
            misc.append(f"- **{comp}**: a reboot is pending (Windows updates)")
    L += misc or ["- nothing abnormal"]
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("reports")
    p.add_argument("--out")
    a = p.parse_args(argv)
    machines, heads = analyse(a.reports)
    text = report(machines, heads)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
