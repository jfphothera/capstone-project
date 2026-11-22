"""The fleet's model catalog, from the operator's plan spreadsheet.

    python -I import_catalog.py <plan.xlsx> --out model-catalog.json [--no-verify]

Source: AI_Model_Deployment_and_Storage_Plan_Dependency_Packages.xlsx, one sheet per hardware
tier. Each row: DOWNLOAD? (YES/NO), model, download GB, representation (quantization), capability,
estimated RAM, runtime, "Reported installed: <tag>" and the pinned Hugging Face source. Only
standard-library code: the hub has no extra packages.

The Ollama name of a row is the library tag the lab already uses when the row says "Reported
installed: <tag>", otherwise hf.co/<pinned GGUF repository>:<quantization>. Rows served by another
runtime (Transformers, SpeechBrain and the like) are kept, marked runtime "other": the Inference
Gateway serves Ollama models only. With verification (the default), every Ollama name is looked
up on registry.ollama.ai or hf.co, and its manifest digest and size are recorded; a name that
does not resolve is marked, never guessed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_library as ml  # noqa: E402

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
TIERS = {  # sheet -> tier id, label, hardware it matches
    "235U 8 GB RAM": ("CPU8", "CPU, 8 GB RAM (Core Ultra 5 235U)", {"kind": "cpu", "ram_gb": [0, 12]}),
    "235U 16 GB RAM": ("CPU16", "CPU, 16 GB RAM (Core Ultra 5 235U)", {"kind": "cpu", "ram_gb": [12, 24]}),
    "235U 32 GB RAM": ("CPU32", "CPU, 32 GB RAM (Core Ultra 5 235U)", {"kind": "cpu", "ram_gb": [24, 4096]}),
    "RTX 4060 Ti Practical": ("GPU16", "GPU, RTX 4060 Ti 16 GB (practical set)", {"kind": "gpu", "vram_gb": [14, 20]}),
    "RTX 4060 Ti Optimized": ("GPU16-OPT", "GPU, RTX 4060 Ti 16 GB (borderline, optimized)", {"kind": "gpu", "vram_gb": [14, 20]}),
    "Future RTX 3090 RTX 4090": ("GPU24", "GPU, 24 GB (RTX 3090 / 4090, future)", {"kind": "gpu", "vram_gb": [20, 4096]}),
}
QUANT = re.compile(r"\b(I?Q\d(?:_[0-9A-Z]+)*|F16|BF16|F32)\b")


def read_sheets(path, wanted):
    """{sheet name: [ {column letter: text} ]} for the wanted sheets, stdlib only."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in wb.find("m:sheets", NS):
        name = s.get("name")
        if name not in wanted:
            continue
        target = rels[s.get("{%s}id" % NS["r"])].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter("{%s}row" % NS["m"]):
            cells = {}
            for c in row.findall("m:c", NS):
                v = c.find("m:v", NS)
                t = c.get("t")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
                else:
                    val = v.text if v is not None else ""
                cells[re.match(r"[A-Z]+", c.get("r")).group(0)] = val
            rows.append(cells)
        out[name] = rows
    return out


def _num(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


# The tier sheets differ (the GPU sheets add VRAM columns), so columns are found by their heading.
FIELDS = {"download": "DOWNLOAD?", "model": "Model / checkpoint", "download_gb": "Download GB",
          "representation": "Actual selected representation", "files": "Exact weight file(s)", "capability": "Capability",
          "parameters_b": "Total parameters B", "ram_gb": "Estimated system RAM high GB", "vram_gb": "Estimated VRAM high GB",
          "runtime": "Recommended runtime", "existing": "Existing / newly recommended", "source": "Pinned download source",
          "key": "Canonical model key", "license": "License / intended use", "retention": "Retention proposal",
          "quality": "Quality / lifecycle"}


def header_map(rows):
    """{field: column letter} from the sheet's heading row (the one whose A is DOWNLOAD?)."""
    head = next((r for r in rows if r.get("A") == "DOWNLOAD?"), None)
    if not head:
        raise ValueError("no heading row (DOWNLOAD?) in a tier sheet")
    out = {}
    for field, title in FIELDS.items():
        col = next((c for c, text in head.items() if (text or "").startswith(title)), None)
        if col:
            out[field] = col
    for need in ("download", "model", "runtime", "source"):
        if need not in out:
            raise ValueError(f"tier sheet has no '{FIELDS[need]}' column")
    return out


def ollama_name(f):
    """(Ollama name or None, runtime 'ollama'|'other', how the name was found) from one row's fields."""
    runtime_text, rep, files = f.get("runtime", ""), f.get("representation", ""), f.get("files", "")
    if (f.get("capability") or "").strip().lower() in ("images", "image", "audio", "video"):
        # Image generators and audio models can ship as GGUF too, but Ollama serves text and embedding models.
        return None, "other", "served by another runtime: " + ((f.get("capability") or "") + " model")
    gguf = "llama.cpp" in runtime_text or "GGUF" in rep or "GGUF" in files or ".gguf" in files.lower()
    reported = re.search(r"Reported installed:\s*([A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)", f.get("existing", ""))
    if reported:
        return reported.group(1), "ollama", "the lab's library tag (reported installed)"
    if not gguf:
        return None, "other", "served by another runtime: " + (runtime_text.split(";")[0] or "unknown")
    repo = re.search(r"huggingface\.co/([^/]+/[^/]+?)(?:/tree/|/?$)", f.get("source", ""))
    quant = QUANT.search(rep) or QUANT.search(files)
    if not repo or not quant:
        return None, "ollama", "GGUF, but its repository or quantization is not in the row"
    return f"hf.co/{repo.group(1)}:{quant.group(1)}", "ollama", "the plan's pinned GGUF file on Hugging Face"


def build_catalog(sheets, source_path):
    models = {}
    for sheet, rows in sheets.items():
        tier = TIERS[sheet][0]
        cols = header_map(rows)
        for r in rows:
            f = {field: (r.get(col) or "") for field, col in cols.items()}
            if f["download"] not in ("YES", "NO") or not f["model"]:
                continue
            key = f.get("key") or f["model"]
            name, runtime, how = ollama_name(f)
            m = models.setdefault(key, {
                "key": key, "source_model": f["model"], "name": name, "runtime": runtime, "name_source": how,
                "capability": f.get("capability") or None, "parameters_b": _num(f.get("parameters_b")),
                "download_gb": _num(f.get("download_gb")), "ram_gb": _num(f.get("ram_gb")), "vram_gb": _num(f.get("vram_gb")),
                "representation": f.get("representation") or None, "license": (f.get("license") or "").split(";")[0] or None,
                "retention": f.get("retention") or None, "quality": (f.get("quality") or "").split(";")[0] or None,
                "source_url": f.get("source") or None, "tiers": {}})
            m["tiers"][tier] = f["download"]
            if m["name"] is None and name:
                m.update(name=name, runtime=runtime, name_source=how)
            if m.get("vram_gb") is None and f.get("vram_gb"):
                m["vram_gb"] = _num(f.get("vram_gb"))
    digest = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    return {"schema": "agent-arena.model-catalog/2", "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"file": Path(source_path).name, "sha256": digest},
            "tiers": {t[0]: {"label": t[1], "match": t[2], "sheet": s} for s, t in TIERS.items() if s in sheets},
            "models": sorted(models.values(), key=lambda m: (m["runtime"] != "ollama", (m["name"] or m["key"]).lower()))}


def verify(catalog, opener=None, workers=12):
    """Look every Ollama name up on its registry: manifest digest and total size, or why not."""
    opener = opener or urllib.request.build_opener()

    def one(m):
        if m["runtime"] != "ollama" or not m["name"]:
            return m, None
        try:
            host, ns, model, tag = ml.parse_name(m["name"])
            req = urllib.request.Request(f"https://{host}/v2/{ns}/{model}/manifests/{tag}",
                                         headers={"Accept": ml.MANIFEST_TYPE, "User-Agent": ml.USER_AGENT})
            with opener.open(req, timeout=40) as r:
                data = r.read()
            _, size = ml.blob_digests(data)
            return m, {"ok": True, "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": size}
        except Exception as exc:  # recorded, never guessed around
            return m, {"ok": False, "error": str(exc)[:160]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for m, v in pool.map(one, catalog["models"]):
            if v is not None:
                m["verified"] = v
    catalog["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return catalog


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("xlsx"); p.add_argument("--out", required=True); p.add_argument("--no-verify", action="store_true")
    a = p.parse_args(argv)
    cat = build_catalog(read_sheets(a.xlsx, set(TIERS)), a.xlsx)
    if not a.no_verify:
        verify(cat)
    Path(a.out).write_text(json.dumps(cat, indent=1, ensure_ascii=False), encoding="utf-8")
    om = [m for m in cat["models"] if m["runtime"] == "ollama"]
    bad = [m for m in om if not (m.get("verified") or {}).get("ok", a.no_verify)]
    print(f"{len(cat['models'])} models: {len(om)} served by Ollama, {len(cat['models']) - len(om)} by other runtimes; "
          f"{len(bad)} Ollama name(s) did not resolve" + ("" if a.no_verify else ""))
    for m in bad:
        print(f"  NOT RESOLVED {m['name'] or m['key']}: {(m.get('verified') or {}).get('error', m['name_source'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
