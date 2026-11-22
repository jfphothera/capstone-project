"""Each agent's look in the rooms: a body from the eight-character family and a palette (Arena 2.4.0, 2026-11-18).

Declared for every Capstone agent (inventory of 2026-11-18): 14 workflow agents on 14 hues ~26 degrees apart, each body
used twice with colors on opposite sides of the wheel; Testing Lab participants on neutral shells with one shared amber
accent, so they read as "lab, not the system under test"; model-call sub-roles in the classic robot with their parent's
hue. Any other agent gets a body and hue from a hash of its key. No brown (the operator's choice). Pure.

Bodies: classic (the original robot SVG), a-round, b-wheel, c-hex, d-wide, e-dome, f-kite, g-tread. The robot cat
(h-robot-cat) is not an agent body: it is the rooms' easter egg.
"""
from __future__ import annotations

import colorsys
import hashlib

BODIES = ("classic", "a-round", "b-wheel", "c-hex", "d-wide", "e-dome", "f-kite", "g-tread")
LAB_ACCENT = "#f2ad4a"

# agent_id -> (body, hue, saturation %, lightness %)
CAPSTONE_WORKFLOW = {
    "safety_complaint": ("b-wheel", 0, 68, 58),
    "ticket_interpretation": ("a-round", 26, 68, 58),
    "coordinator": ("d-wide", 51, 75, 58),
    "reviewed_learning": ("c-hex", 77, 60, 56),
    "documentation": ("g-tread", 103, 55, 52),
    "operations": ("e-dome", 129, 55, 48),
    "ticket_history": ("f-kite", 154, 60, 52),
    "erp_research": ("b-wheel", 180, 65, 45),
    "independent_validation": ("a-round", 206, 70, 60),
    "qms_research": ("c-hex", 231, 65, 58),
    "evidence_join": ("d-wide", 257, 60, 64),
    "resolution_planning": ("g-tread", 283, 55, 58),
    "communication_correction": ("e-dome", 309, 60, 56),
    "communication_review": ("f-kite", 334, 70, 64),
}
# agent_id -> (body, lightness % of the neutral shell)
CAPSTONE_LAB = {
    "lab_reviewer": ("d-wide", 90),
    "lab-customer-simulator": ("a-round", 74),
    "lab-environment-simulator": ("g-tread", 58),
    "testing_lab_probe": ("b-wheel", 42),
    "lab_role_screening": ("c-hex", 66),
}
# Figures Arena derives itself (adapters/capstone/translate.py): chat turns and model-call sub-roles.
CAPSTONE_CHAT = ("classic", 48, 85, 70)
SUB_ROLES = ("supervisor", "peer_reviewer", "reflection", "tool_delegate", "investigation", "customer_communication",
             "retrieval_embedding")


def _hex(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, max(0, min(100, l)) / 100.0, max(0, min(100, s)) / 100.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def tones(h, s=68, l=58):
    """The palette channels of the character family's recolor masks."""
    return {"primary-shade": _hex(h, s, l - 20), "primary-base": _hex(h, s, l), "primary-shine": _hex(h, s - 10, min(92, l + 22)),
            "accent": _hex(h + 180, 75, 62)}


def neutral(l):
    return {"primary-shade": _hex(215, 10, l - 22), "primary-base": _hex(215, 10, l), "primary-shine": _hex(215, 8, min(96, l + 16)),
            "accent": LAB_ACCENT}


def _digest(text):
    return int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)


def appearance(app_id, agent_id):
    """{"body", "palette", "color", "group"}: color is the palette's base (rosters, dots, the dispatch color)."""
    if app_id == "capstone":
        if agent_id in CAPSTONE_WORKFLOW:
            body, h, s, l = CAPSTONE_WORKFLOW[agent_id]
            return _look(body, tones(h, s, l), "workflow")
        if agent_id in CAPSTONE_LAB:
            body, l = CAPSTONE_LAB[agent_id]
            return _look(body, neutral(l), "lab")
        if agent_id == "chat_assistant":
            body, h, s, l = CAPSTONE_CHAT
            return _look(body, tones(h, s, l), "chat")
        parent, _, sub = agent_id.partition(".")
        if sub in SUB_ROLES and parent in CAPSTONE_WORKFLOW:
            _body, h, s, l = CAPSTONE_WORKFLOW[parent]
            return _look("classic", tones(h + 6 * (SUB_ROLES.index(sub) + 1), s, min(80, l + 12)), "sub-role")
    n = _digest(f"{app_id}/{agent_id}")
    h, l = n % 360, (52, 60, 68)[(n >> 9) % 3]
    return _look(BODIES[(n >> 3) % len(BODIES)], tones(h, 62, l), "other")


def _look(body, palette, group):
    return {"body": body, "palette": palette, "color": palette["primary-base"], "group": group}
