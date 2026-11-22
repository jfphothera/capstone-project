"""Address reservations, machine claims and app codes, over the real signed HTTP API.

The service binds a claim to the reserved address. Tests run on 127.0.0.1, so they stand in
for the machine's address through Handler._peer; everything else is the real path."""
import pytest

from arena_service import client as aclient
from arena_service import server


@pytest.fixture
def at(monkeypatch):
    """Make the next requests come from `address`."""
    where = {"address": "127.0.0.1"}
    monkeypatch.setattr(server.Handler, "_peer", lambda self: where["address"])
    return lambda address: where.update(address=address)


def test_next_free_skips_registered_and_reserved(env):
    env.head("head-192-0-2-2", "192.0.2.2")
    env.head("head-192-0-2-3", "192.0.2.3")
    assert env.store.next_free("head") == "192.0.2.4"
    env.store.reserve_address("192.0.2.4", "head")
    assert env.store.next_free("head") == "192.0.2.5"
    assert env.store.next_free("app") == "192.0.2.200"


def test_reservations_follow_the_address_plan(env):
    env.app("capstone", "Ticketing")
    with pytest.raises(ValueError, match="outside the head range"):
        env.store.reserve_address("192.0.2.200", "head")
    with pytest.raises(ValueError, match="outside the app range"):
        env.store.reserve_address("192.0.2.50", "app", app_id="capstone", orchestrator_id="main")
    with pytest.raises(ValueError, match="unknown orchestrator"):
        env.store.reserve_address("192.0.2.200", "app", app_id="coder", orchestrator_id="main")
    env.head("head-192-0-2-3", "192.0.2.3")
    with pytest.raises(ValueError, match="already registered"):
        env.store.reserve_address("192.0.2.3", "head")
    with pytest.raises(ValueError, match="not on 192.0.2.0/24"):
        env.store.reserve_address("198.51.100.20", "head")


def test_a_head_claims_its_address_once_and_only_from_there(env, at):
    code = env.store.reserve_address("192.0.2.11", "head", label="Head 11 (GPU)")
    at("192.0.2.12")
    with pytest.raises(aclient.ArenaError) as refused:
        aclient.claim(env.url, "192.0.2.11", code, "HEAD-ELEVEN", "h" * 32)
    assert refused.value.code == "address_mismatch"
    at("192.0.2.11")
    answer = aclient.claim(env.url, "192.0.2.11", code, "HEAD-ELEVEN", "h" * 32)
    assert answer["role"] == "head" and answer["node_id"] == "head-192-0-2-11"
    assert answer["service_id"] == env.store.meta("service_id")
    display = env.store.display("head-192-0-2-11")
    assert display["address"] == "192.0.2.11" and display["status"] == "active"     # the viewer installer can enroll now
    row = env.store.list_addresses()[0]
    assert (row["status"], row["computer"], row["code_expires_at"]) == ("claimed", "HEAD-ELEVEN", None)
    assert "machine_hash" not in row
    with pytest.raises(aclient.CredentialRefused):
        aclient.claim(env.url, "192.0.2.11", code, "HEAD-ELEVEN", "h" * 32)       # once
    with pytest.raises(ValueError, match="already claimed"):
        env.store.reserve_address("192.0.2.11", "head")


def test_a_new_code_replaces_the_old_one_and_release_frees_the_address(env, at):
    old = env.store.reserve_address("192.0.2.20", "head")
    new = env.store.reserve_address("192.0.2.20", "head")
    at("192.0.2.20")
    with pytest.raises(aclient.CredentialRefused):
        aclient.claim(env.url, "192.0.2.20", old, "X", "h" * 32)
    env.store.release_address("192.0.2.20")
    with pytest.raises(aclient.CredentialRefused):
        aclient.claim(env.url, "192.0.2.20", new, "X", "h" * 32)
    assert env.store.next_free("head") == "192.0.2.2"


def test_app_laptop_claims_then_its_adapter_enrolls_from_there(env, at):
    env.app("capstone", "Ticketing")
    old_cid = env.store.list_credentials()[0]["credential_id"]           # the producer credential env.app issued
    claim = env.store.reserve_address("192.0.2.200", "app", app_id="capstone", orchestrator_id="main")
    at("192.0.2.200")
    answer = aclient.claim(env.url, "192.0.2.200", claim, "CAPSTONE-LAPTOP", "c" * 32)
    assert answer["role"] == "app" and answer["location_id"] == "orch:capstone/main"
    code = env.store.issue_app_code("capstone", "main")
    at("192.0.2.201")
    with pytest.raises(aclient.ArenaError) as refused:
        aclient.enroll_app(env.url, "capstone", "main", code)
    assert refused.value.code == "address_mismatch"                      # bound to the laptop that claimed .200
    at("192.0.2.200")
    credential_id, secret, response = aclient.enroll_app(env.url, "capstone", "main", code)
    assert response["app_id"] == "capstone" and credential_id.startswith("prd-")
    producer = aclient.Client(env.url, credential_id, secret)
    assert producer.publish([env.ev("orchestrator.heartbeat")])["results"][0]["status"] == "accepted"
    creds = {c["credential_id"]: c for c in env.store.list_credentials()}
    assert creds[old_cid]["revoked_at"]                                  # the old adapter's credential stops working
    with pytest.raises(aclient.CredentialRefused):
        aclient.enroll_app(env.url, "capstone", "main", code)


def test_app_code_without_a_claimed_address_works_from_anywhere(env, at):
    env.app("coder", "Coder")
    code = env.store.issue_app_code("coder", "main")
    at("192.0.2.1")                                                      # e.g. the dev laptop
    credential_id, secret, _ = aclient.enroll_app(env.url, "coder", "main", code)
    assert aclient.Client(env.url, credential_id, secret).publish([env.ev("orchestrator.heartbeat")])["results"][0]["status"] == "accepted"
