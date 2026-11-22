"""The admin hub room's installed art is complete and its switch sits on the measured wall panel."""
import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "frontend" / "assets" / "admin-hub-v1"


def files(value):
    if isinstance(value, str) and value.endswith(".png"):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from files(v)
    elif isinstance(value, list):
        for v in value:
            yield from files(v)


def test_every_catalog_file_is_installed():
    catalog = json.loads((ASSETS / "catalog.json").read_text(encoding="utf-8"))
    missing = [f for f in files(catalog) if not (ASSETS / f).is_file()]
    assert not missing, missing
    assert set(catalog["pose_order"]) == set(catalog["poses"])
    for action in catalog["actions"].values():
        assert all(0 <= i < len(catalog["pose_order"]) for i in action["frames"])


def test_switch_is_on_the_flat_panel_right_of_the_door_pipes():
    # Measured 1:1 on room-lit.png: the panel between its bolts spans x 470-520, y 105-260.
    s = json.loads((ASSETS / "catalog.json").read_text(encoding="utf-8"))["switch"]
    (x, y), (w, h) = s["origin_px"], s["size"]
    assert 470 <= x and x + w <= 520 and 105 <= y and y + h <= 260


def test_traffic_board_art_is_installed_with_its_measured_faces():
    board = ASSETS.parent / "admin-traffic-board-v1"
    catalog = json.loads((board / "catalog.json").read_text(encoding="utf-8"))
    missing = [f for f in files(catalog) if not (board / f).is_file()]
    assert not missing, missing
    # Measured 1:1 on housing.png: two flat #05080B faces, 656 x 404, 40 px apart.
    assert catalog["canvas"] == [768, 1024]
    assert catalog["faces"] == {"departures": [56, 120, 656, 404], "arrivals": [56, 564, 656, 404]}
    assert len(catalog["lamps"]["top_left"]) == 3 and set(catalog["lamps"]["states"]) >= {"green", "amber", "red"}
