"""Offline, deterministic tests for the data shaping and for fetch()'s
promise never to raise. iNaturalist is a fake urlopen that answers from
fixtures keyed on the path; the fal.ai painter is never called."""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

import server

TODAY = date(2026, 10, 28)
MELB = {"latitude": -37.81, "longitude": 144.96, "label": "Melbourne"}


def _photo(pid: int, code: str | None, who: str = "Someone") -> dict:
    return {
        "id": pid,
        "url": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{pid}/square.jpg",
        "medium_url": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{pid}/medium.jpg",
        "license_code": code,
        "attribution": f"(c) {who}, some rights reserved ({(code or '').upper()})",
    }


SPECIES = {
    "total_results": 123,
    "results": [
        {
            "count": 1839,
            "taxon": {
                "id": 1,
                "name": "Diodon nicthemerus",
                "preferred_common_name": "Globefish",
                "default_photo": _photo(11, "cc-by-nc-sa", "J. Martin Crossley"),
            },
        },
        {
            "count": 498,
            "taxon": {
                "id": 2,
                "name": "Chrysophrys auratus",
                "preferred_common_name": "Australasian Snapper",
                "default_photo": _photo(22, None, "ellalis"),
            },
        },
        {
            "count": 40,
            "taxon": {
                "id": 3,
                "name": "Girella zebra",
                "preferred_common_name": "Zebrafish",
                "default_photo": _photo(33, None, "nobody"),
            },
        },
        {
            "count": 7,
            "taxon": {
                "id": 4,
                "name": "Nemo nemo",
                "preferred_common_name": "No Photo Fish",
            },
        },
    ],
}

LATEST = {
    1: {
        "observed_on": "2026-10-20",
        "user": {"login": "diver"},
        "photos": [_photo(111, "cc-by-nc", "Diver")],
    },
    2: {
        "observed_on": "2026-10-25",
        "user": {"login": "ellalis"},
        "photos": [_photo(222, "cc-by", "Ella")],
    },
    3: {
        "observed_on": "2025-02-01",
        "user": {"login": "zed"},
        "photos": [_photo(333, "cc0", "Zed")],
    },
}

CURATED = {2: _photo(2222, "cc-by-sa", "Curator")}


# ----- shaping ------------------------------------------------------------


def test_rows_rank_by_count_and_pick_the_best_open_photo() -> None:
    rows = server.species_rows(SPECIES["results"], LATEST, CURATED)
    assert [r["name"] for r in rows] == [
        "Globefish",
        "Australasian Snapper",
        "Zebrafish",
    ]
    assert [r["count"] for r in rows] == [1839, 498, 40]
    # the taxon's own default photo when it is openly licensed
    assert rows[0]["photo"] == {
        "url": "https://inaturalist-open-data.s3.amazonaws.com/photos/11/medium.jpg",
        "by": "J. Martin Crossley",
        "lic": "CC BY NC SA",
    }
    # an all-rights-reserved default gives way to a curated open photo
    assert rows[1]["photo"]["by"] == "Curator" and rows[1]["photo"]["lic"] == "CC BY SA"
    # and, failing that, to the latest record's own photo
    assert rows[2]["photo"]["by"] == "Zed" and rows[2]["photo"]["lic"] == "CC0"
    assert rows[0]["last"] == "2026-10-20"
    # a species with no open photo at all is left off the plate
    assert all(r["sci"] != "Nemo nemo" for r in rows)


def test_open_photo_rejects_closed_licences() -> None:
    assert server.open_photo(_photo(1, None)) is None
    assert server.open_photo(_photo(1, "")) is None
    # NoDerivatives photos may not be painted from, so they are not used at all
    assert server.open_photo(_photo(1, "cc-by-nd")) is None
    assert server.open_photo(_photo(1, "cc-by-nc-nd")) is None
    assert server.open_photo(_photo(1, "cc-by-sa"))["lic"] == "CC BY SA"
    assert server.open_photo({"license_code": "cc0"}) is None  # no url


def test_photo_by_parses_the_attribution_line() -> None:
    assert (
        server.photo_by(
            {
                "attribution": "(c) Marine Explorer (Dr John Turnbull), some rights reserved (CC BY-NC-SA)"
            }
        )
        == "Marine Explorer (Dr John Turnbull)"
    )
    assert server.photo_by({"attribution_name": "Alice"}) == "Alice"
    assert server.photo_by({}, {"login": "bob"}) == "bob"


def test_window_since() -> None:
    assert server.window_since("all", TODAY) == ""
    assert server.window_since("year", TODAY) == "2026-01-01"
    assert server.window_since("90d", TODAY) == "2026-07-30"


def test_base_query_place_or_user() -> None:
    q = server.base_query((-37.81, 144.96), "25", "", "90d", TODAY)
    assert q["lat"] == "-37.8100" and q["radius"] == "25" and q["d1"] == "2026-07-30"
    assert q["iconic_taxa"] == "Actinopterygii" and q["quality_grade"] == "research"
    assert (
        q["photo_license"]
        == server.LICENCES
        == "cc0,cc-by,cc-by-nc,cc-by-sa,cc-by-nc-sa"
    )
    u = server.base_query((-37.81, 144.96), "25", "someone", "all", TODAY)
    assert u["user_login"] == "someone" and "lat" not in u and "d1" not in u


def test_title_for() -> None:
    assert (
        server.title_for({"waters": "the Yarra"}, "Melbourne", "")
        == "Fishes of the Yarra"
    )
    assert server.title_for({}, "Melbourne", "") == "Fishes near Melbourne"
    assert server.title_for({}, "", "diver") == "Fishes seen by diver"
    assert (
        server.title_for({"title": "My plate", "waters": "x"}, "Melbourne", "")
        == "My plate"
    )


def test_roman_and_day_words() -> None:
    assert [server.roman(n) for n in (1, 4, 9, 14, 24)] == [
        "I",
        "IV",
        "IX",
        "XIV",
        "XXIV",
    ]
    assert server.day_words("2026-10-28", TODAY) == "to-day"
    assert server.day_words("2026-10-20", TODAY) == "8 days ago"
    assert server.day_words("2026-03-02", TODAY) == "2 March"
    assert server.day_words("2024-03-02", TODAY) == "March 2024"
    assert server.day_words("nonsense", TODAY) == ""


def test_prompt_asks_for_the_plate_pose() -> None:
    text = server.prompt_for(
        {"style": "bloch", "common": "Globefish", "sci": "Diodon nicthemerus"}
    )
    for words in (
        "whole fish alone",
        "lateral view facing left",
        "fins spread",
        "every scale and every fin ray drawn",
        "plain pure white paper",
        "no water",
        "no background",
        "no text",
        "no shadow",
        "Bloch",
    ):
        assert words in text
    mono = server.prompt_for({"style": "gyotaku", "common": "x", "sci": "y"})
    assert "black ink only" in mono
    assert set(server.STYLES) == {"bloch", "gyotaku", "watercolour", "woodcut", "line"}
    assert (
        server.painting_key("Diodon nicthemerus", "bloch")
        == "diodon_nicthemerus--bloch--p1"
    )


# ----- fetch, offline -------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=0):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if "/observations/species_counts" in url:
        return _Resp(json.dumps(SPECIES).encode())
    if "/observations?" in url:
        tid = int(url.split("taxon_id=")[1].split("&")[0])
        return _Resp(
            json.dumps({"results": [LATEST[tid]] if tid in LATEST else []}).encode()
        )
    if "/taxa/" in url:
        tid = int(url.rstrip("/").rsplit("/", 1)[1])
        photo = CURATED.get(tid)
        return _Resp(
            json.dumps(
                {"results": [{"taxon_photos": [{"photo": photo}] if photo else []}]}
            ).encode()
        )
    raise OSError("no network in tests")


def test_fetch_shapes_the_plate(tmp_path: Path) -> None:
    opts = {**MELB, "count": "3", "pictures": "photo", "waters": "Port Phillip Bay"}
    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        d = server.fetch(opts, {}, ctx={"data_dir": str(tmp_path), "preview": True})
    assert d["state"] == "ok"
    assert d["title"] == "Fishes of Port Phillip Bay"
    assert d["plate"].startswith("Plate ")
    assert [s["name"] for s in d["species"]] == [
        "Globefish",
        "Australasian Snapper",
        "Zebrafish",
    ]
    # the photos could not be fetched (no network), so the rows carry no picture
    assert all(s["pic"] is None for s in d["species"])
    assert d["records"] == 1839 + 498 + 40
    assert "within 25 km of Melbourne" in d["foot"]
    assert (tmp_path / "cache").exists()


def test_fetch_serves_the_cache_without_network(tmp_path: Path) -> None:
    opts = {**MELB, "count": "2"}
    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        first = server.fetch(opts, {}, ctx={"data_dir": str(tmp_path)})
    assert first["state"] == "ok"

    def _down(req, timeout=0):
        raise OSError("down")

    with patch.object(urllib.request, "urlopen", _down):
        again = server.fetch(opts, {}, ctx={"data_dir": str(tmp_path)})
        cold = server.fetch(opts, {}, ctx={"data_dir": str(tmp_path / "other")})
    assert [s["sci"] for s in again["species"]] == [s["sci"] for s in first["species"]]
    assert cold["state"] == "error" and cold["error"] == server.ERR_UNREACHABLE


def test_fetch_without_a_place_or_user_says_so(tmp_path: Path) -> None:
    d = server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert d["state"] == "error" and d["error"] == server.ERR_NOWHERE


def test_fetch_never_raises(tmp_path: Path) -> None:
    with patch.object(server, "cached_inat", side_effect=RuntimeError("boom")):
        d = server.fetch(dict(MELB), {}, ctx={"data_dir": str(tmp_path)})
    assert d["state"] == "error" and "error" in d


@pytest.mark.parametrize("window", ["all", "year", "90d"])
def test_fetch_empty_state_is_honest(tmp_path: Path, window: str) -> None:
    def _none(req, timeout=0):
        return _Resp(json.dumps({"results": [], "total_results": 0}).encode())

    with patch.object(urllib.request, "urlopen", _none):
        d = server.fetch(
            {**MELB, "window": window}, {}, ctx={"data_dir": str(tmp_path)}
        )
    assert d["state"] == "empty"
    assert "No research-grade fish records" in d["error"]
    assert "Melbourne" in d["error"]
