"""Fish Plate: the fishes of the waters around the panel as a natural-history
plate. The species come from iNaturalist's research-grade records within a
radius of the place (or one user's records), ranked by how often each has been
recorded; each carries its best openly licensed photo, the photographer and
licence, its latest record and its count. Pictures are the photographs, or AI
paintings of each fish made once per style through the host's app.artwork
collection and kept.

Everything from the network is cached under the widget's data dir for six
hours, and fetch() never raises: it returns {"error": "..."} instead.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from app import artwork  # Tesserae 0.463+: the pictures a widget makes and keeps
    from app import fal as _fal
except ImportError:  # an older Tesserae: photographs only, with a note saying why
    artwork = None  # type: ignore[assignment]
    _fal = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

WIDGET = "fish_plate"
USER_AGENT = (
    "tesserae/fish_plate/0.1.0 (+https://github.com/dmellok/tesserae-widget-fish-plate)"
)
INAT = "https://api.inaturalist.org/v1"
# The photo licences the plate may use. No NoDerivatives licence: an AI painting
# made from a photo is a derivative work, which CC BY-ND and CC BY-NC-ND forbid.
# The same list filters the species counts, each species' latest record and the
# taxon's curated photos.
LICENCES = "cc0,cc-by,cc-by-nc,cc-by-sa,cc-by-nc-sa"
LICENCE_SET = frozenset(LICENCES.split(","))
HTTP_TIMEOUT_S = 15
IMG_TIMEOUT_S = 40
IMG_MAX_BYTES = 12_000_000
CACHE_S = 6 * 3600
MISS_S = 86400
SPECIES_MAX = 5
WINDOWS = ("all", "year", "90d")
WINDOW_WORDS = {"all": "all time", "year": "this year", "90d": "the last 90 days"}
RADII = ("25", "5", "10", "50", "100")
LAYOUTS = ("stack", "hero")
PAPERS = ("cream", "white", "aged")
LEVELS = ("colour", "gray16", "gray4", "mono")
PICTURES = ("photo", "gen")
PICTURE_PX = 900
HERO_PX = 1400
ASPECT = "3:2"
LIVE_S = 6
NEEDS_HOST = "AI paintings need Tesserae 0.463 or later; this server is older, so the photographs show."
ERR_NOWHERE = (
    "Set a place in this cell (or the app's location), or an iNaturalist username."
)
ERR_UNREACHABLE = "iNaturalist could not be reached; the plate shows again when it can."
ERR_BUSY = "iNaturalist answered {err}; the plate shows again when it can."
ERR_SHAPE = "iNaturalist's answer was not in the expected shape."

# Each style: its label, the words the painter gets, and whether it is black ink only.
STYLES: dict[str, tuple[str, str, bool]] = {
    "bloch": (
        "Hand-coloured engraving after Bloch",
        "a hand-coloured copperplate engraving in the manner of Marcus Elieser Bloch's "
        "Allgemeine Naturgeschichte der Fische of 1785: fine engraved linework and stippling "
        "under translucent watercolour washes, every scale a small engraved crescent, "
        "a little gilt where the fish is silvery",
        False,
    ),
    "gyotaku": (
        "Gyotaku ink print",
        "a gyotaku fish print: the fish itself inked in black sumi and pressed onto paper, "
        "the scales and fin rays showing as the texture of the rubbing, the eye brushed in "
        "afterwards, black ink only with no colour at all",
        True,
    ),
    "watercolour": (
        "Watercolour",
        "a modern watercolour study: soft wet washes for the body, a few precise dry-brush "
        "details on the scales, eye and fins, plenty of untouched white paper",
        False,
    ),
    "woodcut": (
        "Woodcut",
        "a woodcut print: bold carved black lines and flat planes of two or three muted "
        "colours, the grain of the block showing in the flat areas, the fish's markings "
        "simplified but true",
        False,
    ),
    "line": (
        "Scientific line drawing",
        "a scientific line drawing in black ink for a fish identification guide: clean even "
        "outlines, the rows of scales and the fin rays counted and drawn, light stippled "
        "shading, black ink only with no colour at all",
        True,
    ),
}
MODELS = ("nano_banana", "nano_banana_pro", "flux_kontext", "gpt_image", "seedream")
MODEL_LABELS = {
    "nano_banana": "Nano Banana",
    "nano_banana_pro": "Nano Banana Pro",
    "flux_kontext": "FLUX.1 Kontext Pro",
    "gpt_image": "GPT Image 2",
    "seedream": "Seedream 4",
}
PROMPT = (
    "Paint the {name} ({sci}) in the reference photo as {style}. Show the whole fish alone, "
    "in lateral view facing left, fins spread, every scale and every fin ray drawn, its true "
    "colours, markings and proportions kept exactly as the species has them{mono}. "
    "The fish fills the picture from nose to tail. On plain pure white paper: no water, "
    "no background, no text, no label, no border, no shadow."
)
PROMPT_MONO = ", rendered in black ink only"

# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60] or "x"


def roman(n: int) -> str:
    out, n = "", max(1, min(3999, int(n)))
    for value, glyph in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        while n >= value:
            out, n = out + glyph, n - value
    return out


def _write(path: Path, data: bytes) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _choice(value: Any, allowed: tuple[str, ...]) -> str:
    v = str(value if value is not None else "").strip()
    return v if v in allowed else allowed[0]


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def http(
    url: str, accept: str = "application/json", timeout: float = HTTP_TIMEOUT_S
) -> tuple[bytes | None, str | None]:
    """(bytes, None) or (None, why): "unreachable", "too large" or "HTTP <code>"."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(IMG_MAX_BYTES + 1)
            return (None, "too large") if len(data) > IMG_MAX_BYTES else (data, None)
    except urllib.error.HTTPError as err:
        return None, f"HTTP {err.code}"
    except Exception:
        return None, "unreachable"


def get_json(url: str) -> tuple[Any, str | None]:
    raw, err = http(url)
    if err:
        return None, err
    try:
        return json.loads((raw or b"").decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        return None, "bad json"


# ----------------------------------------------------------------------
# iNaturalist
# ----------------------------------------------------------------------


def window_since(window: str, today: date) -> str:
    """The d1 date of a window, or "" for all time."""
    if window == "year":
        return date(today.year, 1, 1).isoformat()
    if window == "90d":
        return (today - timedelta(days=90)).isoformat()
    return ""


def base_query(
    place: tuple[float, float] | None,
    radius_km: str,
    login: str,
    window: str,
    today: date,
) -> dict[str, str]:
    """The filters shared by every call: fishes, research grade, an open photo."""
    q = {
        "iconic_taxa": "Actinopterygii",
        "quality_grade": "research",
        "photos": "true",
        "photo_license": LICENCES,
        "locale": "en",
    }
    since = window_since(window, today)
    if since:
        q["d1"] = since
    if login:
        q["user_login"] = login
    elif place:
        q["lat"], q["lng"], q["radius"] = (
            f"{place[0]:.4f}",
            f"{place[1]:.4f}",
            radius_km,
        )
    return q


def licence_label(code: str) -> str:
    code = str(code or "").lower()
    return "CC0" if code == "cc0" else code.upper().replace("-", " ") if code else ""


def photo_by(photo: dict[str, Any] | None, user: dict[str, Any] | None = None) -> str:
    """Who took a photo: its attribution name, else the "(c) Name," of its
    attribution line, else the observer."""
    photo = photo or {}
    who = str(photo.get("attribution_name") or "").strip()
    if not who:
        attr = str(photo.get("attribution") or "")
        m = re.match(r"\(c\)\s*(.+?),\s*(some|all|no) rights reserved", attr)
        who = m.group(1).strip() if m else ""
    if not who and user:
        who = str(user.get("name") or user.get("login") or "").strip()
    return who


def _medium(url: str) -> str:
    return re.sub(r"/(square|small|thumb|large|original)\.", "/medium.", str(url or ""))


def open_photo(
    photo: dict[str, Any] | None, user: dict[str, Any] | None = None
) -> dict[str, str] | None:
    """{url, by, lic} of a photo record when it is openly licensed, else None."""
    if not isinstance(photo, dict):
        return None
    code = str(photo.get("license_code") or "").lower()
    url = str(photo.get("medium_url") or photo.get("url") or "")
    if code not in LICENCE_SET or not url:
        return None
    return {
        "url": _medium(url),
        "by": photo_by(photo, user),
        "lic": licence_label(code),
    }


def species_rows(
    species: list[dict[str, Any]],
    latest: dict[int, dict[str, Any]],
    curated: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The plate's rows from the species counts and, per taxon, its latest
    observation: the best open photo (the taxon's own default photo when it is
    openly licensed, else the first open one among the taxon's curated photos,
    else the latest record's), the photographer and licence, the latest date
    and the count. A species with no open photo is left out."""
    out: list[dict[str, Any]] = []
    for s in species:
        t = s.get("taxon") or {}
        tid = int(t.get("id") or 0)
        sci = str(t.get("name") or "").strip()
        if not tid or not sci:
            continue
        common = str(t.get("preferred_common_name") or "").strip()
        obs = latest.get(tid) or {}
        photos = obs.get("photos") if isinstance(obs.get("photos"), list) else []
        own = next(
            (p for p in (open_photo(p, obs.get("user")) for p in photos) if p), None
        )
        pic = (
            open_photo(t.get("default_photo"))
            or open_photo((curated or {}).get(tid))
            or own
        )
        if not pic:
            continue
        out.append(
            {
                "taxon_id": tid,
                "sci": sci,
                "common": common,
                "name": common or sci,
                "count": int(s.get("count") or 0),
                "last": str(obs.get("observed_on") or ""),
                "photo": pic,
            }
        )
    return out


def curated_photo(taxon_id: int) -> dict[str, Any] | None:
    """The first openly licensed photo among a taxon's curated photos on
    iNaturalist, the ones its page shows first, or None."""
    got, err = get_json(f"{INAT}/taxa/{int(taxon_id)}")
    rows = got.get("results") if isinstance(got, dict) and not err else None
    if not rows:
        return None
    for tp in rows[0].get("taxon_photos") or []:
        photo = tp.get("photo") if isinstance(tp, dict) else None
        if open_photo(photo):
            return photo
    return None


def fetch_inat(q: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """The species counts for a query and, for each of the top species, its
    latest observation (one small call per species)."""
    sq = urllib.parse.urlencode({**q, "per_page": str(SPECIES_MAX)})
    found, err = get_json(f"{INAT}/observations/species_counts?{sq}")
    if err == "unreachable":
        return None, ERR_UNREACHABLE
    if err:
        return None, ERR_BUSY.format(err=err)
    if not isinstance(found, dict) or not isinstance(found.get("results"), list):
        return None, ERR_SHAPE
    species = found["results"][:SPECIES_MAX]
    latest: dict[str, Any] = {}
    curated: dict[str, Any] = {}
    for s in species:
        t = s.get("taxon") or {}
        tid = int(t.get("id") or 0)
        if not tid:
            continue
        if not open_photo(t.get("default_photo")):
            photo = curated_photo(tid)
            if photo:
                curated[str(tid)] = photo
        oq = urllib.parse.urlencode(
            {
                **q,
                "taxon_id": str(tid),
                "order_by": "observed_on",
                "order": "desc",
                "per_page": "1",
            }
        )
        obs, err = get_json(f"{INAT}/observations?{oq}")
        rows = obs.get("results") if isinstance(obs, dict) and not err else None
        if rows:
            latest[str(tid)] = rows[0]
    return {
        "species": species,
        "species_total": int(found.get("total_results") or 0),
        "latest": latest,
        "curated": curated,
        "at": time.time(),
    }, None


def cached_inat(
    data_dir: Path, q: dict[str, str], fresh: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """The iNaturalist answer for a query, kept for six hours."""
    key = hashlib.sha1(json.dumps(q, sort_keys=True).encode()).hexdigest()[:20]
    path = data_dir / "cache" / f"inat_{key}.json"
    hit = _read_json(path, None)
    if (
        not fresh
        and isinstance(hit, dict)
        and time.time() - float(hit.get("at") or 0) < CACHE_S
    ):
        return hit, None
    got, err = fetch_inat(q)
    if got is None:
        if isinstance(
            hit, dict
        ):  # stale is better than nothing while iNaturalist is away
            return hit, None
        return None, err
    _write(path, json.dumps(got).encode("utf-8"))
    return got, None


# ----------------------------------------------------------------------
# Pictures
# ----------------------------------------------------------------------


def cached_image(data_dir: Path, url: str) -> bytes | None:
    """A photo's bytes, fetched once and kept; a miss is remembered for a day."""
    folder = data_dir / "img"
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path, miss = folder / f"{key}.bin", folder / f"{key}.miss"
    if path.exists():
        with contextlib.suppress(OSError):
            return path.read_bytes()
    with contextlib.suppress(OSError):
        if miss.exists() and time.time() - miss.stat().st_mtime < MISS_S:
            return None
    body, err = http(url, accept="image/*", timeout=IMG_TIMEOUT_S)
    if err or not body:
        with contextlib.suppress(OSError):
            folder.mkdir(parents=True, exist_ok=True)
            miss.touch()
        return None
    _write(path, body)
    return body


def render_photo(raw: bytes, px: int, cache: Path, levels: str) -> str:
    """A photo fitted to ``px`` on its longest side as a JPEG data URI, converted
    and contrast-stretched for a greyscale or black-and-white panel; cached."""
    cache = cache.with_name(f"{cache.stem}_{px}_{levels}.jpg")
    if cache.exists():
        with contextlib.suppress(OSError):
            return (
                "data:image/jpeg;base64,"
                + base64.b64encode(cache.read_bytes()).decode()
            )
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with Tesserae
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        return ""
    im.thumbnail((px, px), Image.Resampling.LANCZOS)
    if levels != "colour":
        cutoff = {"gray16": 0.5, "gray4": 1.0, "mono": 1.5}.get(levels, 1.0)
        im = ImageOps.autocontrast(im.convert("L"), cutoff=cutoff)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    data = out.getvalue()
    _write(cache, data)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def photo_picture(
    row: dict[str, Any], data_dir: Path, levels: str, px: int
) -> dict[str, Any] | None:
    pic = row.get("photo") or {}
    raw = cached_image(data_dir, pic["url"]) if pic.get("url") else None
    if not raw:
        return None
    key = hashlib.sha1(pic["url"].encode()).hexdigest()[:20]
    src = render_photo(raw, px, data_dir / "img" / f"{key}.jpg", levels)
    if not src:
        return None
    return {
        "src": src,
        "kind": "photo",
        "credit": f"Photo by {pic.get('by') or 'an iNaturalist observer'} ({pic.get('lic')}), iNaturalist",
    }


# ----------------------------------------------------------------------
# AI paintings, through the host's collection
# ----------------------------------------------------------------------


def prompt_for(payload: dict[str, Any]) -> str:
    style = STYLES.get(str(payload.get("style") or ""), STYLES["bloch"])
    return PROMPT.format(
        name=payload.get("common") or payload.get("sci") or "fish",
        sci=payload.get("sci") or "",
        style=style[1],
        mono=PROMPT_MONO if style[2] else "",
    )


def paint(job: Any) -> tuple[bytes, dict[str, Any]]:
    """One painting on a worker thread: the species' photo as the reference,
    the style's words, the picture from fal.ai through the collection's budget."""
    p = dict(job.payload)
    photo = str(p.get("photo") or "")
    if not photo:
        raise artwork.ArtworkError("no openly licensed reference photo of the species")
    model = str(p.get("model") or "") if p.get("model") in MODELS else "nano_banana"
    raw = job.fal.generate(model, prompt_for(p), refs=[photo], aspect=ASPECT)
    label = MODEL_LABELS.get(model, model)
    by, lic = str(p.get("by") or "an iNaturalist observer"), str(p.get("lic") or "")
    credit = f"AI painting ({label} on fal.ai) from a photo by {by}{f' ({lic})' if lic else ''}"
    return raw, {
        "credit": credit,
        "style": str(p.get("style") or "bloch"),
        "sci": str(p.get("sci") or ""),
        "common": str(p.get("common") or ""),
        "model": model,
    }


def cut_to_fish(raw: bytes) -> bytes | None:
    """The painting cropped to the fish, its paper made pure white. The painter
    leaves a margin of off-white paper round the fish: on the plate that margin
    would show as a pale rectangle (the plate's own paper is cream) and make
    the fish small. The paper colour is the median of the border pixels; every
    channel is scaled so that colour becomes white, and the box is every pixel
    that still differs from white by more than a little, with a small border."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with Tesserae
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    if w < 32 or h < 32:
        return None
    px = im.load()
    border = [px[x, y] for x in range(0, w, 5) for y in (1, h - 2)]
    border += [px[x, y] for y in range(0, h, 5) for x in (1, w - 2)]
    paper = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    if min(paper) < 160:  # not on paper at all: leave the picture as it is
        return None
    bands = im.split()
    white = Image.merge(
        "RGB",
        [
            b.point([min(255, round(v * 255 / max(1, paper[i]))) for v in range(256)])
            for i, b in enumerate(bands)
        ],
    )
    dark = white.convert("L").point(lambda v: 255 if v < 225 else 0)
    box = dark.getbbox()
    if not box:
        return None
    pad = max(4, int(0.025 * max(w, h)))
    x0, y0 = max(0, box[0] - pad), max(0, box[1] - pad)
    x1, y1 = min(w, box[2] + pad), min(h, box[3] + pad)
    if x1 - x0 < w * 0.15 or y1 - y0 < h * 0.1:
        return None
    out = io.BytesIO()
    white.crop((x0, y0, x1, y1)).save(out, "PNG", optimize=True)
    return out.getvalue()


def aspect_of(path: Path | None) -> float:
    """Width over height of a picture file, or 1.5 when it cannot be read; the
    page lays a row out from it before the pictures have decoded."""
    if path is None:
        return 1.5
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        return round(w / h, 3) if w and h else 1.5
    except Exception:
        return 1.5


def painting_src(art: Any, key: str, data_dir: Path, px: int, levels: str) -> str:
    """The kept painting as a data URI, cut to the fish once and then derived
    per size and panel by the collection; the whole picture when the cut fails."""
    folder = ART.folder(data_dir)
    cut = folder / f"{key}__cut.png"
    kept = ART.path(key, data_dir) if hasattr(ART, "path") else None
    stale = False
    with contextlib.suppress(OSError):
        stale = (
            bool(kept) and cut.exists() and kept.stat().st_mtime > cut.stat().st_mtime
        )
    if stale or not cut.exists():
        got = art.picture(key)
        data = cut_to_fish(got[0]) if got else None
        if data:
            _write(cut, data)
    if cut.exists():
        src = art.url(key, px=px, levels=levels, variant="cut")
        if src:
            return src
    return art.url(key, px=px, levels=levels)


class _NoArt:
    """What the collection opens to on a Tesserae without app.artwork: every
    request answers no_host, nothing starts, the note says why."""

    def __init__(self) -> None:
        self.asked = 0

    def request(self, key: str, payload: dict[str, Any], *, title: str = "") -> Any:
        from types import SimpleNamespace

        self.asked += 1
        return SimpleNamespace(
            key=key, state="no_host", ready=False, meta={}, why=NEEDS_HOST
        )

    def url(self, *a: Any, **k: Any) -> str:
        return ""

    def picture(self, *a: Any, **k: Any) -> None:
        return None

    def live(self) -> dict[str, int]:
        return {"live_refresh_s": 0, "live_pending": 0}

    def notes(self) -> list[str]:
        return [NEEDS_HOST] if self.asked else []

    def status(self) -> list[str]:
        return []

    def finish(self, *a: Any, **k: Any) -> None:
        return None


class _NoCollection:
    def folder(self, data_dir: Path) -> Path:
        return Path(data_dir) / "plates"

    def open(
        self, ctx: dict[str, Any], settings: dict[str, Any] | None, *, subject: str = ""
    ) -> _NoArt:
        return _NoArt()


ART: Any = (
    artwork.Collection(
        WIDGET,
        "plates",
        make=paint,
        label="Fish Plate",
        title="Painting fishes",
        facet="style",
        facet_label=lambda s: STYLES.get(s, ("AI painting",))[0],
        default_cents=200,
    )
    if artwork is not None
    else _NoCollection()
)


def painting_key(sci: str, style: str) -> str:
    return f"{slug(sci)}--{style}--p1"


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------


def _place(options: dict[str, Any]) -> tuple[float, float, str] | None:
    loc = options.get("location") if isinstance(options.get("location"), dict) else {}
    try:
        lat = float(options.get("latitude", loc.get("latitude")))
        lon = float(options.get("longitude", loc.get("longitude")))
    except (TypeError, ValueError):
        return None
    name = options.get("label") or loc.get("name") or loc.get("label") or ""
    return lat, lon, str(name).strip()


def title_for(options: dict[str, Any], place_name: str, login: str) -> str:
    custom = str(options.get("title") or "").strip()
    if custom:
        return custom
    waters = str(options.get("waters") or "").strip()
    if waters:
        return f"Fishes of {waters}"
    if login:
        return f"Fishes seen by {login}"
    return f"Fishes near {place_name}" if place_name else "Fishes near here"


def day_words(iso: str, today: date) -> str:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    days = (today - d).days
    if days <= 0:
        return "to-day"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if d.year == today.year:
        return d.strftime("%-d %B")
    return d.strftime("%B %Y")


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    try:
        return _fetch(options or {}, settings or {}, ctx or {})
    except Exception as err:
        logger.exception("fish_plate: fetch failed")
        return {
            "state": "error",
            "error": "The plate could not be drawn right now.",
            "detail": str(err)[:200],
        }


def _fetch(
    options: dict[str, Any], settings: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    data_dir = Path(ctx.get("data_dir") or ".")
    today = date.today()
    source = _choice(options.get("source"), ("place", "user"))
    login = (
        re.sub(r"[^A-Za-z0-9_.-]", "", str(options.get("inat_user") or ""))
        if source == "user"
        else ""
    )
    place = _place(options)
    radius = _choice(options.get("radius_km"), RADII)
    window = _choice(options.get("window"), WINDOWS)
    count = _int(options.get("count"), 3, 1, SPECIES_MAX)
    layout = _choice(options.get("layout"), LAYOUTS)
    paper = _choice(options.get("paper"), PAPERS)
    levels = _choice(options.get("panel"), LEVELS)
    pictures = _choice(options.get("pictures"), PICTURES)
    style = _choice(options.get("style"), tuple(STYLES))
    model = _choice(options.get("model"), MODELS)
    credits = str(options.get("credits", True)).lower() not in (
        "false",
        "0",
        "no",
        "off",
    )
    place_name = place[2] if place else ""
    area = (
        f"within {radius} km of {place_name or 'the place'}"
        if not login
        else f"recorded by {login}"
    )
    base: dict[str, Any] = {
        "title": title_for(options, place_name, login),
        "subtitle": "",
        "plate": "",
        "layout": layout,
        "paper": paper,
        "panel": levels,
        "pictures": pictures,
        "style": style,
        "credits": credits,
        "area": area,
        "waters": str(options.get("waters") or "").strip(),
        "foot": area,
        "period": WINDOW_WORDS[window],
        "species": [],
        "notices": [],
        "status": [],
        "live_refresh_s": 0,
        "live_pending": 0,
    }
    if not login and not place:
        return {**base, "state": "error", "error": ERR_NOWHERE}

    q = base_query(
        (place[0], place[1]) if place else None, radius, login, window, today
    )
    got, err = cached_inat(data_dir, q, bool(ctx.get("fresh")))
    if got is None:
        return {**base, "state": "error", "error": err or ERR_UNREACHABLE}
    latest = {
        int(k): v for k, v in (got.get("latest") or {}).items() if str(k).isdigit()
    }
    curated = {
        int(k): v for k, v in (got.get("curated") or {}).items() if str(k).isdigit()
    }
    rows = species_rows(got.get("species") or [], latest, curated)[:count]
    plate_no = zlib.crc32(json.dumps(q, sort_keys=True).encode()) % 24 + 1
    base["plate"] = f"Plate {roman(plate_no)}"
    base["species_total"] = int(got.get("species_total") or 0)
    if not rows:
        what = (
            "this year"
            if window == "year"
            else "in the last 90 days"
            if window == "90d"
            else ""
        )
        return {
            **base,
            "state": "empty",
            "error": f"No research-grade fish records with an open photo {area}{f' {what}' if what else ''}.",
        }

    # Pictures: the photo, or the painting when it is ready, with the photo meanwhile.
    gen = pictures == "gen"
    art = (
        ART.open(
            ctx,
            settings,
            subject=f"{slug(login or place_name or 'here')}-{radius}-{window}",
        )
        if gen
        else None
    )
    for i, row in enumerate(rows):
        px = HERO_PX if (layout == "hero" and i == 0) else PICTURE_PX
        row["pic"] = photo_picture(row, data_dir, levels, px)
        row["last_words"] = day_words(row["last"], today) if row.get("last") else ""
        if art is not None:
            key = painting_key(row["sci"], style)
            payload = {
                "style": style,
                "photo": row["photo"]["url"],
                "common": row["common"],
                "sci": row["sci"],
                "by": row["photo"].get("by", ""),
                "lic": row["photo"].get("lic", ""),
                "model": model,
            }
            got_art = art.request(key, payload, title=row["name"])
            if got_art.ready:
                src = painting_src(art, key, data_dir, px, levels)
                if src:
                    cut = ART.folder(data_dir) / f"{key}__cut.png"
                    row["pic"] = {
                        "src": src,
                        "kind": "painting",
                        "credit": str(got_art.meta.get("credit") or "AI painting"),
                        "aspect": aspect_of(cut if cut.exists() else got_art.path),
                    }
    if art is not None:
        art.finish(ctx, facet=style)
        base["notices"] = art.notes()
        base["status"] = art.status()
        base.update(art.live())
    n = sum(r["count"] for r in rows)
    base["records"] = n
    if gen and style == "bloch":
        base["subtitle"] = "After Marcus Elieser Bloch, 1785"
        base["foot"] = f"{area} · {n:,} records"
    else:
        base["subtitle"] = (
            f"{n:,} research-grade records on iNaturalist, {WINDOW_WORDS[window]}"
        )
        base["foot"] = area
    base["species"] = [
        {
            "name": r["name"],
            "sci": r["sci"],
            "count": r["count"],
            "last": r.get("last_words", ""),
            "pic": r["pic"],
        }
        for r in rows
    ]
    return {**base, "state": "ok"}
