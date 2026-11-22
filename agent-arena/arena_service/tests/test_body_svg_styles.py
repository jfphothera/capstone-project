"""Agent Arena 2.2.1: the classic robot's repaint rule must never reach the eight-character family's bodies.

2026-11-18: agent_arena.css painted every path inside a figure with the agent's color
(`.actor svg path:not(.screen)... {fill:var(--agent)}`). The live room page (arena-room.html) loads it, so every new
body lost its dark outline and face screen; the white Testing Lab figures showed as faceless ghosts on the heads.
"""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "frontend" / "css"
PAGE = Path(__file__).resolve().parents[2] / "frontend" / "arena-room.html"


def rules(text):
    return [m.group(0) for m in re.finditer(r"[^{}]+\{[^{}]*\}", re.sub(r"/\*.*?\*/", "", text, flags=re.S))]


def test_no_stylesheet_of_the_live_room_page_repaints_the_new_bodies():
    sheets = re.findall(r'href="css/([^"]+\.css)"', PAGE.read_text(encoding="utf-8"))
    assert "agent_arena.css" in sheets and "scene_composer.css" in sheets, "the stylesheets the viewers load"
    offenders = []
    for name in sheets:
        for rule in rules((CSS / name).read_text(encoding="utf-8")):
            selectors, body = rule.split("{", 1)
            if "fill" not in body:
                continue
            for sel in selectors.split(","):
                sel = sel.strip()
                broad = re.search(r"(\.actor|\.avatar)\b[^,]*\bsvg\b[^,]*\s(path|rect|\*)\b", sel) or re.search(r"\.avatar\s+(path|rect|\*)\b", sel)
                if broad and ":not(.body-svg)" not in sel:
                    offenders.append(f"{name}: {sel}")
    assert not offenders, offenders


def test_the_new_bodies_carry_their_own_fixed_colors():
    svg = (Path(__file__).resolve().parents[2] / "frontend" / "assets" / "characters-v2" / "bots" / "d-wide" / "front" / "idle.svg").read_text(encoding="utf-8")
    assert 'class="palette fixed"' in svg and "var(--primary-base" in svg
