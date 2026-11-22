"""Inference Gateway observer: shared.inference.observation/1 -> the admin console (no gateway imports).

Read-only. It reads the sanitized observation document the gateway's operator publishes, never the
gateway's ledger, and publishes nothing to the event service: gateway capacity activity stays apart
from applications' arena.v2 workflow telemetry.
"""
