"""Authenticated, sanitized Gateway 0.3 deployment verification."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shared_inference_client import Client


def verify(path: str, expected_application: str) -> dict:
    client = Client.from_config(path)
    if client.app != expected_application:
        raise ValueError(f"Expected {expected_application} Gateway identity")
    models = client.models()
    policy = client.policy()
    if models.get("contract") != "shared.inference/1" or policy.get("contract") != "shared.inference/1":
        raise ValueError("Unexpected Gateway contract")
    if policy.get("application_id") != expected_application:
        raise PermissionError("Gateway returned policy for a different application")
    model_names = sorted({
        model
        for resource in models.get("resources", [])
        for model in resource.get("models", [])
    })
    availability = None
    if model_names:
        availability = client.availability(model_names[0], "/api/generate")
    return {
        "application_id": expected_application,
        "authenticated": True,
        "contract": models.get("contract"),
        "service_id": client.service_id,
        "authorized_model_count": len(model_names),
        "policy_enabled": (policy.get("policy") or {}).get("enabled"),
        "sample_availability_status": (availability or {}).get("status"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operational", required=True)
    parser.add_argument("--research", required=True)
    args = parser.parse_args()
    result = {"format": "capstone-gateway-0.3-verification-v1", "verified": False}
    try:
        result["identities"] = [
            verify(args.operational, "capstone"),
            verify(args.research, "capstone-research"),
        ]
        result["verified"] = True
    except Exception as exc:
        result.update(status="offline_or_unverified", error_class=type(exc).__name__)
    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
