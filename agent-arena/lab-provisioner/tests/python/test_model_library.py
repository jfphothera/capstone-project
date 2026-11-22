"""The hub's model library: names, the files it keeps, and the registry answers Ollama's pull needs.

Field proof (2026-11-06, head 6, Ollama 0.40.13): smollm:135m pulled from this library over the lab
cable kept the registry's manifest digest sha256:b0b2a461..., was renamed with /api/copy, answered a
generate, and was deleted again.
"""
import hashlib
import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine" / "hub-tools" / "model-library"))
import model_library as ml  # noqa: E402


def test_names_round_trip():
    assert ml.parse_name("gemma3:4b") == ("registry.ollama.ai", "library", "gemma3", "4b")
    assert ml.parse_name("all-minilm") == ("registry.ollama.ai", "library", "all-minilm", "latest")
    assert ml.parse_name("alibayram/smollm3:latest") == ("registry.ollama.ai", "alibayram", "smollm3", "latest")
    assert ml.parse_name("hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M") == ("hf.co", "unsloth", "SmolLM3-3B-GGUF", "Q4_K_M")
    for n in ("gemma3:4b", "alibayram/smollm3:latest", "hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M"):
        assert ml.display_name(*ml.parse_name(n)) == n
    assert ml.served_path("hf.co", "unsloth", "SmolLM3-3B-GGUF") == "hfco-unsloth/SmolLM3-3B-GGUF"
    # Ollama refuses a dot in a namespace: "invalid model name", HTTP 400 (heads 2 and 10, 2026-11-09)
    assert "." not in ml.served_path("hf.co", "my.org", "M-1.5B-GGUF").split("/")[0]
    for bad in ("", "a/b/c/d:e", "../x:y", "x y:z"):
        with pytest.raises(ValueError):
            ml.parse_name(bad)


def test_allow_list():
    assert ml.parse_allow("192.0.2.2-4,192.0.2.6") == {"192.0.2.2", "192.0.2.3", "192.0.2.4", "192.0.2.6"}


def make_library(tmp_path):
    lib = ml.Library(tmp_path / "lib")
    weights = b"W" * 300000
    config = b'{"model_format":"gguf"}'
    d_w = "sha256:" + hashlib.sha256(weights).hexdigest()
    d_c = "sha256:" + hashlib.sha256(config).hexdigest()
    lib.blob_path(d_w).write_bytes(weights)
    lib.blob_path(d_c).write_bytes(config)
    manifest = json.dumps({"schemaVersion": 2, "mediaType": ml.MANIFEST_TYPE,
                           "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "digest": d_c, "size": len(config)},
                           "layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": d_w, "size": len(weights)}]}).encode()
    p = lib.manifest_path("registry.ollama.ai", "library", "tiny", "1b")
    p.parent.mkdir(parents=True)
    p.write_bytes(manifest)
    return lib, manifest, d_w, weights


def test_index_lists_complete_models_with_the_registry_digest(tmp_path):
    lib, manifest, d_w, _ = make_library(tmp_path)
    [item] = lib.index()
    assert item["name"] == "tiny:1b" and item["served"] == "library/tiny:1b" and item["complete"]
    assert item["digest"] == "sha256:" + hashlib.sha256(manifest).hexdigest()
    lib.blob_path(d_w).unlink()
    assert not lib.index()[0]["complete"]


@pytest.fixture()
def served(tmp_path):
    lib, manifest, d_w, weights = make_library(tmp_path)
    srv = ml.build_server(lib, "127.0.0.1", 0, {"127.0.0.1"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], manifest, d_w, weights
    srv.shutdown()
    srv.server_close()


def get(port, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_registry_answers_ollama_needs(served):
    port, manifest, d_w, weights = served
    r, body = get(port, "/v2/")
    assert r.status == 200
    r, body = get(port, "/v2/library/tiny/manifests/1b")
    assert r.status == 200 and body == manifest and r.getheader("Content-Type") == ml.MANIFEST_TYPE
    assert r.getheader("Docker-Content-Digest") == "sha256:" + hashlib.sha256(manifest).hexdigest()
    # The blob GET must redirect to another host name (Ollama refuses a same-host answer).
    r, _ = get(port, f"/v2/library/tiny/blobs/{d_w}")
    assert r.status == 307 and r.getheader("Location") == f"http://[::ffff:127.0.0.1]:{port}/direct/{d_w}"
    r, body = get(port, f"/v2/library/tiny/blobs/{d_w}", method="HEAD")
    assert r.status == 200 and int(r.getheader("Content-Length")) == len(weights)
    r, body = get(port, f"/direct/{d_w}", {"Range": "bytes=100-199"})
    assert r.status == 206 and body == weights[100:200] and r.getheader("Content-Range") == f"bytes 100-199/{len(weights)}"
    r, body = get(port, f"/direct/{d_w}")
    assert r.status == 200 and body == weights
    assert get(port, "/v2/library/nothing/manifests/1b")[0].status == 404
    assert get(port, "/direct/sha256:" + "0" * 64)[0].status == 404


def test_only_allowed_addresses_are_served(tmp_path):
    lib, *_ = make_library(tmp_path)
    srv = ml.build_server(lib, "127.0.0.1", 0, {"192.0.2.6"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv.server_address[1], "/v2/library/tiny/manifests/1b")[0].status == 403
    finally:
        srv.shutdown()
        srv.server_close()
