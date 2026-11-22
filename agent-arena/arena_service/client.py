"""Reference arena.v2 client: request signing and response verification.

Used by the Ticketing adapter, the synthetic Coder producer and the tests, and
the specification the Windows desktop bridge implements.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import CONTRACT, crypto


class ArenaError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class ServiceIdentityError(ArenaError):
    """The response was not signed by a service that knows this credential."""


class CredentialRefused(ArenaError):
    """An unsigned 401/403: the service does not know this credential, so it cannot sign the refusal.

    Nothing from such a response is used; it only explains the failure more truthfully
    than "not signed". A successful response must still be signed.
    """


class RateLimited(ArenaError):
    """A 429: too many requests. The service may refuse before it authenticates (so unsigned);
    nothing from the response is used except how long to wait."""

    def __init__(self, status, code, message, retry_after=2.0):
        super().__init__(status, code, message)
        self.retry_after = retry_after


class Client:
    def __init__(self, base_url, credential_id, secret, *, timeout=30):
        self.base = base_url.rstrip("/")
        self.credential_id = credential_id
        self.key = crypto.derive_key(secret)
        self.timeout = timeout

    def request(self, method, target, payload=None, *, timeout=None):
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        stamp, nonce = str(int(time.time())), crypto.b64u(secrets.token_bytes(16))
        path = urlsplit(self.base).path.rstrip("/") + target
        headers = {"X-Arena-Credential": self.credential_id, "X-Arena-Timestamp": stamp, "X-Arena-Nonce": nonce,
                   "X-Arena-Signature": crypto.sign(self.key, crypto.canonical_request(method, path, stamp, nonce, body)),
                   "Content-Type": "application/json"}
        request = urllib.request.Request(self.base + target, data=body if method == "POST" else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                status, raw, headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status, raw, headers = exc.code, exc.read(), exc.headers
        signature = headers.get("X-Arena-Response-Signature")
        if not signature or not crypto.verify(self.key, crypto.canonical_response(status, nonce, raw), signature):
            if status == 429:
                retry = _seconds(headers.get("Retry-After")) or 2.0
                raise RateLimited(status, "rate_limited", f"Too many requests; retry after {retry:g} s", retry)
            if status in (401, 403):
                raise CredentialRefused(status, "refused",
                                        "The service refused this credential (unknown, revoked, expired or already used)")
            raise ServiceIdentityError(status, "unsigned_response",
                                       "Response is not signed by the Arena Event Service for this credential")
        data = json.loads(raw) if raw else {}
        if status >= 400:
            raise ArenaError(status, data.get("error", "error"), data.get("message", ""))
        return data

    def publish(self, events):
        return self.request("POST", "/v2/events", {"contract": CONTRACT, "events": events})

    def snapshot(self, view="cluster"):
        return self.request("GET", "/v2/snapshot?view=" + view)

    def events(self, after, view="cluster", wait=0):
        return self.request("GET", f"/v2/events?after={after}&view={view}&wait={wait}",
                            timeout=max(self.timeout, wait + 10))


def _seconds(value):
    try:
        return max(0.0, min(60.0, float(value)))
    except (TypeError, ValueError):
        return None


def enroll(base_url, display_id, code, machine_hash, timeout=30):
    """Exchange a one-time enrollment code for a viewer credential (id, secret, response)."""
    client = Client(base_url, "enroll:" + display_id, crypto.normalize_code(code), timeout=timeout)
    try:
        response = client.request("POST", "/v2/enroll", {"machine_hash": machine_hash})
    except CredentialRefused as exc:
        raise CredentialRefused(exc.status, "code_refused",
                                f"Enrollment code refused for {display_id}: already used, expired, replaced by a newer "
                                "code, or issued for a different display") from None
    secret = crypto.unseal(client.key, response["sealed_secret"]).decode()
    return response["credential_id"], secret, response


def claim(base_url, address, code, computer, machine_hash, timeout=30):
    """A newly provisioned machine takes its reserved address with the one-time claim code. Must be
    sent from that address. Returns the service's answer (role, node or app identity, endpoints)."""
    client = Client(base_url, "claim:" + address, crypto.normalize_code(code), timeout=timeout)
    try:
        return client.request("POST", "/v2/claim", {"computer": computer, "machine_hash": machine_hash})
    except CredentialRefused as exc:
        raise CredentialRefused(exc.status, "code_refused", f"Claim code refused for {address}: already used, expired, "
                                "replaced by a newer code, or reserved for a different address") from None


def enroll_app(base_url, app_id, orchestrator_id, code, timeout=30):
    """Exchange a one-time app code for this application's producer credential (id, secret, response)."""
    client = Client(base_url, f"app:{app_id}/{orchestrator_id}", crypto.normalize_code(code), timeout=timeout)
    try:
        response = client.request("POST", "/v2/app/enroll", {})
    except CredentialRefused as exc:
        raise CredentialRefused(exc.status, "code_refused", f"App code refused for {app_id}/{orchestrator_id}: already "
                                "used, expired or replaced by a newer code") from None
    secret = crypto.unseal(client.key, response["sealed_secret"]).decode()
    return response["credential_id"], secret, response


def iso(epoch=None):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat()
