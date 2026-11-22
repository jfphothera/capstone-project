"""The hub's model library: download a model once, keep its original files, serve exact copies to heads.

    python -I model_library.py fetch  --library <folder> <model> [<model> ...]
    python -I model_library.py serve  --library <folder> --bind 192.0.2.254 --port 11500 [--allow 192.0.2.2-199]
    python -I model_library.py list   --library <folder>

A model is its manifest plus the blobs it names (weights, template, parameters, licence). The
heads' Ollama identifies a model by the SHA-256 of its manifest, so the library keeps the
manifest bytes exactly as the registry sent them: a head that pulls from here gets the
identical model, the same digest, and the same Inference Gateway qualification identity
(checked 2026-11-06 against heads 3 and 6, for library and hf.co models).

Upstreams: registry.ollama.ai for library names (gemma3:4b, user/model:tag), hf.co for
hf.co/<user>/<repo>:<quant>. Every blob is checked against its digest before it is kept.

Serve speaks the read-only part of the registry API that Ollama's pull uses (/v2/,
manifests, blobs with Range), over plain HTTP on the lab network, to the allowed head
addresses only. A head pulls with
    POST http://<head>:11434/api/pull {"model": "<hub>:<port>/<served name>", "insecure": true}
and is then given the model's normal name with /api/copy. Nothing is uploaded or deleted
through serve.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
USER_AGENT = "agent-arena-model-library/1.0"


def ca_bundles():
    """Mozilla's public root list as this Python already carries it (inside pip: certifi's cacert.pem). Pure."""
    return [p for p in (Path(sys.prefix) / "Lib" / "site-packages" / "pip" / "_vendor" / "certifi" / "cacert.pem",
                        Path(sys.base_prefix) / "Lib" / "site-packages" / "pip" / "_vendor" / "certifi" / "cacert.pem")
            if p.is_file()]


def internet_opener():
    """HTTPS to the model registries with Windows' trusted roots plus Mozilla's public root list. The hub's Windows
    root store lacks the roots registry.ollama.ai and hf.co chain to (it rarely reaches the internet, so Windows never
    fetched them): "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate" on DESKTOP-REDACT10, 2026-11-08 and
    11-09. Certificates are still verified; nothing is trusted that Mozilla's list does not trust."""
    import ssl
    ctx = ssl.create_default_context()
    for bundle in ca_bundles()[:1]:
        ctx.load_verify_locations(cafile=str(bundle))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


# ---------------------------------------------------------------------------- names (pure, tested)

def parse_name(name):
    """A model name as Ollama lists it -> (upstream host, namespace path, model, tag).

    gemma3:4b -> registry.ollama.ai, library, gemma3, 4b
    user/m:t -> registry.ollama.ai, user, m, t
    hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M -> hf.co, unsloth, SmolLM3-3B-GGUF, Q4_K_M
    """
    name = (name or "").strip()
    base, _, tag = name.rpartition(":") if ":" in name.split("/")[-1] else (name, "", "latest")
    tag = tag or "latest"
    parts = base.split("/")
    if parts[0] in ("hf.co", "huggingface.co") and len(parts) == 3:
        host, ns, model = "hf.co", parts[1], parts[2]
    elif len(parts) == 1:
        host, ns, model = "registry.ollama.ai", "library", parts[0]
    elif len(parts) == 2:
        host, ns, model = "registry.ollama.ai", parts[0], parts[1]
    else:
        raise ValueError(f"not a model name this library knows: {name!r}")
    for s in (ns, model, tag):
        if not SEGMENT.match(s):
            raise ValueError(f"not a model name this library knows: {name!r}")
    return host, ns, model, tag


def display_name(host, ns, model, tag):
    """The name Ollama lists for it (the inverse of parse_name)."""
    if host == "hf.co":
        return f"hf.co/{ns}/{model}:{tag}"
    return f"{model}:{tag}" if ns == "library" else f"{ns}/{model}:{tag}"


def served_path(host, ns, model):
    """The repository path heads ask the library for: library models keep library/<model>; hf.co models
    are served under hfco-<user>, so they cannot collide with an Ollama namespace of the same name.
    Ollama refuses a dot in a namespace ("invalid model name", HTTP 400: heads 2 and 10, Ollama 0.40.12/13,
    2026-11-09), so the served namespace has none; the head is then given the model's own hf.co/ name."""
    return f"{ns}/{model}" if host == "registry.ollama.ai" else "hfco-" + ns.replace(".", "-") + f"/{model}"


def blob_digests(manifest):
    doc = json.loads(manifest)
    digests = [doc["config"]["digest"]] + [layer["digest"] for layer in doc.get("layers", [])]
    for d in digests:
        if not DIGEST.match(d):
            raise ValueError("manifest names a blob with an invalid digest")
    return digests, sum(int(layer.get("size", 0)) for layer in doc.get("layers", [])) + int(doc["config"].get("size", 0))


# ---------------------------------------------------------------------------- the library on disk

class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.manifests = self.root / "manifests"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)

    def blob_path(self, digest):
        if not DIGEST.match(digest):
            raise ValueError("invalid digest")
        return self.blobs / digest.replace(":", "-")

    def manifest_path(self, host, ns, model, tag):
        return self.manifests / host / ns / model / tag

    def index(self):
        """Every model kept: its name, served path, manifest digest, size and when it was fetched."""
        out = []
        for p in sorted(self.manifests.rglob("*")):
            if not p.is_file() or p.suffix == ".json":
                continue
            rel = p.relative_to(self.manifests).parts
            if len(rel) != 4:
                continue
            host, ns, model, tag = rel
            data = p.read_bytes()
            digests, size = blob_digests(data)
            out.append({"name": display_name(host, ns, model, tag), "served": f"{served_path(host, ns, model)}:{tag}",
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": size,
                        "complete": all(self.blob_path(d).is_file() for d in digests),
                        "fetched_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
        return out

    def find_served(self, repo, ref):
        """(manifest bytes, digest) for a served repository path and a tag or digest, or None."""
        for item in self.index():
            path, _, tag = item["served"].rpartition(":")
            if path == repo and (ref == tag or ref == item["digest"]):
                host, ns, model, t = parse_name(item["name"])
                return self.manifest_path(host, ns, model, t).read_bytes(), item["digest"]
        return None

    # -- fetch
    def fetch(self, name, say=print, opener=None):
        host, ns, model, tag = parse_name(name)
        opener = opener or internet_opener()
        base = f"https://{host}/v2/{ns}/{model}"
        req = urllib.request.Request(f"{base}/manifests/{tag}", headers={"Accept": MANIFEST_TYPE, "User-Agent": USER_AGENT})
        with opener.open(req, timeout=60) as r:
            manifest = r.read()
        digests, size = blob_digests(manifest)
        say(f"{display_name(host, ns, model, tag)}: manifest sha256:{hashlib.sha256(manifest).hexdigest()[:12]}, "
            f"{len(digests)} file(s), {size / 1e9:.2f} GB")
        for d in digests:
            target = self.blob_path(d)
            if target.is_file() and _sha256_file(target) == d:
                say(f"  {d[:19]} kept already")
                continue
            part = target.with_suffix(".partial")
            req = urllib.request.Request(f"{base}/blobs/{d}", headers={"User-Agent": USER_AGENT})
            h = hashlib.sha256()
            with opener.open(req, timeout=120) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total and done - last > total / 10:
                        say(f"  {d[:19]} {done * 100 // total}%")
                        last = done
            if "sha256:" + h.hexdigest() != d:
                part.unlink(missing_ok=True)
                raise ValueError(f"{d[:19]} did not match its digest; nothing kept")
            os.replace(part, target)
            say(f"  {d[:19]} downloaded and checked")
        mp = self.manifest_path(host, ns, model, tag)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_bytes(manifest)        # written last: a model is listed only once every blob is in place
        return {"name": display_name(host, ns, model, tag), "digest": "sha256:" + hashlib.sha256(manifest).hexdigest(), "size": size}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------- serving to heads

def parse_allow(spec):
    """'192.0.2.2-199' or a comma list of addresses / ranges -> a set of allowed addresses."""
    allowed = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+\.\d+\.\d+\.)(\d+)-(\d+)$", part)
        if m:
            for n in range(int(m.group(2)), int(m.group(3)) + 1):
                allowed.add(f"{m.group(1)}{n}")
        else:
            allowed.add(str(ipaddress.IPv4Address(part)))
    return allowed


ROUTE = re.compile(r"^/v2/(?P<repo>.+)/(?P<kind>manifests|blobs)/(?P<ref>[^/]+)$")
DIRECT = re.compile(r"^/direct/(?P<ref>sha256:[0-9a-f]{64})$")


class Handler(BaseHTTPRequestHandler):
    library: Library = None
    allowed: set = set()
    log_lines = []
    server_version = "AgentArenaModelLibrary/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        line = time.strftime("%H:%M:%S ") + self.client_address[0] + " " + (fmt % args)
        Handler.log_lines.append(line)
        print(line, flush=True)

    def _deny(self):
        if self.allowed and self.client_address[0] not in self.allowed:
            self._send(403, b'{"errors":[{"code":"DENIED","message":"not a head address"}]}', "application/json")
            return True
        return False

    def _send(self, status, body=b"", ctype="application/octet-stream", headers=None, head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        if self._deny():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/v2", "/v2/"):
            return self._send(200, b"{}", "application/json", head=head)
        d = DIRECT.match(path)
        m = ROUTE.match(path)
        if not m and not d:
            return self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}', "application/json", head=head)
        if d:
            ref = d.group("ref")
        else:
            repo, kind, ref = m.group("repo"), m.group("kind"), m.group("ref")
            if kind == "manifests":
                found = self.library.find_served(repo, ref)
                if not found:
                    return self._send(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', "application/json", head=head)
                data, digest = found
                return self._send(200, data, MANIFEST_TYPE, {"Docker-Content-Digest": digest}, head=head)
            if not head:
                # Ollama's downloader asks the registry for the blob's storage location: it follows
                # redirects that stay on the same host name and takes the first one to ANOTHER host name
                # as the location (registry.ollama.ai sends one to its storage servers). Ending on the
                # same host is refused with "no Location header in response" (head 6, Ollama 0.40.13).
                # So the location names this same IPv4 address in its IPv6-mapped form, which is a
                # different host name to Ollama, while the connection still goes to this address.
                if not DIGEST.match(ref) or not self.library.blob_path(ref).is_file():
                    return self._send(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}', "application/json")
                ip, port = self.server.server_address[0], self.server.server_address[1]
                return self._send(307, b"", headers={"Location": f"http://[::ffff:{ip}]:{port}/direct/{ref}"})
        try:
            blob = self.library.blob_path(ref)
        except ValueError:
            return self._send(400, b'{"errors":[{"code":"DIGEST_INVALID"}]}', "application/json", head=head)
        if not blob.is_file():
            return self._send(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}', "application/json", head=head)
        size = blob.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            r = re.match(r"^bytes=(\d+)-(\d*)$", rng.strip())
            if not r:
                return self._send(416, b"", headers={"Content-Range": f"bytes */{size}"}, head=head)
            start = int(r.group(1))
            end = min(int(r.group(2)) if r.group(2) else size - 1, size - 1)
            if start > end:
                return self._send(416, b"", headers={"Content-Range": f"bytes */{size}"}, head=head)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Docker-Content-Digest", ref)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with open(blob, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def build_server(library, bind, port, allowed):
    Handler.library, Handler.allowed = library, allowed
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--library", required=True); f.add_argument("models", nargs="+")
    s = sub.add_parser("serve"); s.add_argument("--library", required=True); s.add_argument("--bind", required=True)
    s.add_argument("--port", type=int, default=11500); s.add_argument("--allow", default="192.0.2.2-199")
    ls = sub.add_parser("list"); ls.add_argument("--library", required=True)
    a = p.parse_args(argv)
    lib = Library(a.library)
    if a.action == "fetch":
        for m in a.models:
            print(json.dumps(lib.fetch(m)))
        return 0
    if a.action == "list":
        print(json.dumps(lib.index(), indent=2))
        return 0
    server = build_server(lib, a.bind, a.port, parse_allow(a.allow))
    print(f"model library serving {len(lib.index())} model(s) on http://{a.bind}:{a.port} to {a.allow}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
