"""The options panel stays short: a handful of options up front, the rest
nested under the choice they depend on or under More options."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8")
)
OPTIONS = MANIFEST["cell_options"]

# Saved cells key on these names; the order is how the panel reads.
ORDER = [
    "location",
    "source",
    "inat_user",
    "radius_km",
    "count",
    "layout",
    "pictures",
    "style",
    "window",
    "waters",
    "paper",
    "panel",
    "model",
    "title",
    "credits",
]

UP_FRONT = ["location", "source", "count", "layout", "pictures"]


def _up_front(o: dict) -> bool:
    return not o.get("show_if") and not o.get("advanced")


def test_manifest_basics() -> None:
    assert MANIFEST["name"] == "Fish Plate"
    assert MANIFEST["kind"] == "widget"
    assert MANIFEST["icon"] == "ph-fish"
    assert MANIFEST["category"] == "art"
    assert MANIFEST["tesserae_min"] == "0.463.0"
    assert MANIFEST["requires"] == ["network:*"]
    assert MANIFEST["render"]["needs_network"] is True
    assert MANIFEST["render"]["warm_ahead_s"] == 150


def test_option_names_and_order() -> None:
    assert [o["name"] for o in OPTIONS] == ORDER


def test_few_options_up_front() -> None:
    assert [o["name"] for o in OPTIONS if _up_front(o)] == UP_FRONT
    assert len(UP_FRONT) <= 5


def test_no_option_named_label() -> None:
    assert "label" not in {o["name"] for o in OPTIONS}


def test_show_if_refers_to_real_options() -> None:
    names = {o["name"] for o in OPTIONS}
    for o in OPTIONS:
        for key in o.get("show_if") or {}:
            assert key in names, (o["name"], key)


def test_advanced_options_come_last() -> None:
    seen = False
    for o in OPTIONS:
        if o.get("advanced"):
            seen = True
        else:
            assert not seen, f"{o['name']} follows an advanced option"


def test_title_is_advanced() -> None:
    title = next(o for o in OPTIONS if o["name"] == "title")
    assert title["type"] == "string" and title.get("advanced") is True


def test_settings_block() -> None:
    names = [s["name"] for s in MANIFEST["settings"]]
    assert names == ["fal_api_key", "gen_daily_usd", "gen_per_refresh"]
    key = MANIFEST["settings"][0]
    assert key["secret"] is True
    budget = MANIFEST["settings"][1]
    assert budget["type"] == "slider" and budget["default"] == 2


def test_no_em_dashes_anywhere() -> None:
    text = json.dumps(MANIFEST, ensure_ascii=False)
    assert chr(0x2014) not in text
    for word in ("first-class", "tinkerers", "seamless", "powerful", "unlike "):
        assert word not in text.lower()
