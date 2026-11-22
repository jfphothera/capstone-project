"""HMAC request signing, response signing and one-time secret sealing.

Secrets never travel on the wire. Both sides derive K = HMAC(secret, "arena.v2")
and sign a canonical request; the service stores only K.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

DOMAIN = b"arena.v2"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I, O, 0, 1: safe to read aloud


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), DOMAIN, hashlib.sha256).digest()


def new_secret() -> str:
    return b64u(secrets.token_bytes(32))


def new_code(groups: int = 4) -> str:
    """Typed once by an operator: 4x4 characters from a 32-symbol alphabet (80 bits)."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(groups * 4))
    return "ARENA-" + "-".join(body[i:i + 4] for i in range(0, len(body), 4))


def normalize_code(code: str) -> str:
    raw = "".join(c for c in code.upper() if c.isalnum())
    if raw.startswith("ARENA"):
        raw = raw[5:]
    return "ARENA-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def canonical_request(method: str, target: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join((method.upper(), target, timestamp, nonce,
                      hashlib.sha256(body).hexdigest())).encode()


def sign(key: bytes, message: bytes) -> str:
    return b64u(hmac.new(key, message, hashlib.sha256).digest())


def verify(key: bytes, message: bytes, signature: str) -> bool:
    try:
        expected = hmac.new(key, message, hashlib.sha256).digest()
        return hmac.compare_digest(expected, unb64u(signature))
    except (ValueError, TypeError):
        return False


def canonical_response(status: int, request_nonce: str, body: bytes) -> bytes:
    return "\n".join((str(status), request_nonce, hashlib.sha256(body).hexdigest())).encode()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(key: bytes, plaintext: bytes) -> str:
    """Encrypt-then-MAC with HMAC-SHA256 keystream; used once to deliver a credential."""
    enc_key = hmac.new(key, b"seal-enc", hashlib.sha256).digest()
    mac_key = hmac.new(key, b"seal-mac", hashlib.sha256).digest()
    nonce = secrets.token_bytes(16)
    cipher = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, nonce, len(plaintext))))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return b64u(nonce + cipher + tag)


def unseal(key: bytes, token: str) -> bytes:
    raw = unb64u(token)
    nonce, cipher, tag = raw[:16], raw[16:-32], raw[-32:]
    enc_key = hmac.new(key, b"seal-enc", hashlib.sha256).digest()
    mac_key = hmac.new(key, b"seal-mac", hashlib.sha256).digest()
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()):
        raise ValueError("sealed payload failed authentication")
    return bytes(a ^ b for a, b in zip(cipher, _keystream(enc_key, nonce, len(cipher))))
