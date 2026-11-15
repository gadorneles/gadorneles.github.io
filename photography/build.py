#!/usr/bin/env python3
"""Build the photography section of the site from Google Drive (or a local folder).

Expected layout (Drive or local):

    Photography/                 <- root folder (its Drive ID goes in trips.yaml)
        Brasil/                  <- country (English or Portuguese name, or ISO code)
            João Pessoa/         <- place: a city or state, used as the album's map location
                2026/            <- year: one album = place + year -> /photography/joao-pessoa-2026/
                    DSCF1234.jpg
                    trip.yaml    <- optional: title, description, location ...
        Japan/
            2025/                <- a year straight under the country also works ("Japan 2025")

Usage (from the repository root):

    python photography/build.py                           # Drive, using trips.yaml + credentials
    python photography/build.py --credentials ~/keys/photos-sa.json
    python photography/build.py --source ~/Pictures/Photography   # local folder instead
    python photography/build.py --dry-run                 # report only, write nothing

Drive credentials (service account JSON), first match wins:
    env GDRIVE_SERVICE_ACCOUNT_JSON (the JSON text)   <- used by GitHub Actions
    --credentials FILE, env GOOGLE_APPLICATION_CREDENTIALS, or settings.credentials

Output (all generated, not committed):
    static/photos/<trip>/<photo>.webp and <photo>-t.webp   web images, metadata stripped
    data/photography.json                                  the "database" read by Hugo
    .photo-cache/manifest.json                             lets later runs skip unchanged photos

See photography/README.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yaml
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    sys.exit("Missing dependencies. Run: pip install -r photography/requirements.txt")

try:  # optional: phone photos in HEIC format
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC = True
except ImportError:
    HEIC = False

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
OUT_IMG = REPO / "static" / "photos"
OUT_JSON = REPO / "data" / "photography.json"
CACHE_DIR = REPO / ".photo-cache"
MANIFEST = CACHE_DIR / "manifest.json"
STATES_FILE = HERE / "states.json"  # Natural Earth admin-1 shapes, one TopoJSON per country
OUT_STATES = OUT_IMG / "_states"
MANIFEST_VERSION = 2

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"} | ({".heic", ".heif"} if HEIC else set())
TRIP_FILES = {"trip.yaml", "trip.yml"}

DEFAULTS = {
    "drive_folder_id": "",
    "source": "",
    "credentials": "",
    "max_size": 2048,  # long edge of the full image, px
    "thumb_height": 540,  # thumbnail height, px (gallery rows are ~270px at 2x)
    "thumb_max_width": 1200,
    "quality": 82,  # WebP quality
    "coord_decimals": 3,  # ~100 m. Lower it (2 = ~1 km) for more privacy
    "place_aliases": {},
}
RENDER_KEYS = ("max_size", "thumb_height", "thumb_max_width", "quality")


# --------------------------------------------------------------------------- helpers


def log(msg: str = "") -> None:
    print(msg, flush=True)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def norm_name(text: str) -> str:
    return slugify(text).replace("-", " ")


DATE_PREFIX = re.compile(r"^(\d{4})(?:[-_. ](\d{1,2}))?(?:[-_. ](\d{1,2}))?(?=$|[\s_-])[\s_-]*")


def split_date(name: str) -> tuple[str | None, str]:
    """'2025-07 Kyoto' -> ('2025-07-01', 'Kyoto'); 'Kyoto' -> (None, 'Kyoto')."""
    m = DATE_PREFIX.match(name.strip())
    if not m or not (1900 < int(m.group(1)) < 2200):
        return None, name.strip()
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        date = dt.date(y, mo, d).isoformat()
    except ValueError:
        date = None
    rest = re.sub(r"_+", " ", name.strip()[m.end():]).strip(" -_")
    return date, rest


_COUNTRIES = json.loads((HERE / "countries.json").read_text(encoding="utf-8"))
COUNTRY_NAMES: dict[str, str] = _COUNTRIES["names"]
COUNTRY_ALIASES: dict[str, str] = _COUNTRIES["aliases"]


def country_name(cc: str | None) -> str | None:
    return COUNTRY_NAMES.get(cc, cc) if cc else None


def country_from_folder(name: str) -> str | None:
    """'Japan', 'Japão', 'JP', 'Estados Unidos' -> ISO code."""
    return COUNTRY_ALIASES.get(norm_name(name))


# --------------------------------------------------------------------------- sources


class LocalSource:
    """A folder on this computer."""

    def __init__(self, root: Path):
        self.root = root
        if not root.is_dir():
            sys.exit(f"Source folder not found: {root}")

    def describe(self) -> str:
        return str(self.root)

    def walk(self) -> list[dict]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or any(part.startswith(".") for part in p.relative_to(self.root).parts):
                continue
            st = p.stat()
            out.append({"key": p.relative_to(self.root).as_posix(), "name": p.name,
                        "fp": f"{st.st_size}-{st.st_mtime_ns}", "path": p})
        return out

    def fetch(self, f: dict, tmp: Path) -> Path:
        return f["path"]

    def read_text(self, f: dict) -> str:
        return f["path"].read_text(encoding="utf-8")


class DriveSource:
    """A Google Drive folder, read with a service account (read-only)."""

    API = "https://www.googleapis.com/drive/v3"
    FOLDER = "application/vnd.google-apps.folder"
    SHORTCUT = "application/vnd.google-apps.shortcut"
    FIELDS = "id,name,mimeType,md5Checksum,size,modifiedTime,shortcutDetails"

    def __init__(self, folder_id: str, creds_info: dict | None = None, session_factory=None):
        self.folder_id = folder_id.strip()
        self.email = (creds_info or {}).get("client_email", "the service account")
        if session_factory is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                creds_info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
            session_factory = lambda: AuthorizedSession(creds)  # noqa: E731
        self._factory = session_factory
        self._local = threading.local()

    @property
    def s(self):
        if not hasattr(self._local, "s"):
            self._local.s = self._factory()
        return self._local.s

    def _get(self, path: str, **params):
        params.setdefault("supportsAllDrives", "true")
        for attempt in range(5):
            r = self.s.get(f"{self.API}{path}", params=params, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def describe(self) -> str:
        r = self._get(f"/files/{self.folder_id}", fields="id,name,mimeType")
        if r.status_code == 404:
            sys.exit(f"Drive folder {self.folder_id} not found. Share it with {self.email} (Viewer) "
                     "and check drive_folder_id in photography/trips.yaml.")
        if r.status_code in (401, 403):
            sys.exit(f"Google Drive refused access ({r.status_code}): {r.text[:300]}\n"
                     "Check that the Google Drive API is enabled for the service account's project "
                     "and that the GDRIVE_SERVICE_ACCOUNT_JSON secret holds the whole key file.")
        r.raise_for_status()
        meta = r.json()
        if meta.get("mimeType") != self.FOLDER:
            sys.exit(f"drive_folder_id points to '{meta.get('name')}', which is not a folder.")
        return f"Google Drive folder '{meta['name']}' ({self.folder_id})"

    def _children(self, parent: str) -> list[dict]:
        items, token = [], None
        while True:
            params = {"q": f"'{parent}' in parents and trashed = false",
                      "fields": f"nextPageToken, files({self.FIELDS})",
                      "pageSize": 1000, "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = self._get("/files", **params)
            r.raise_for_status()
            data = r.json()
            items += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return items

    def walk(self) -> list[dict]:
        out, queue, seen = [], [(self.folder_id, "")], set()
        while queue:
            fid, prefix = queue.pop(0)
            if fid in seen:
                continue
            seen.add(fid)
            for it in self._children(fid):
                if it["name"].startswith("."):
                    continue
                if it["mimeType"] == self.SHORTCUT:  # follow shortcuts to folders and files
                    sd = it.get("shortcutDetails") or {}
                    if sd.get("targetMimeType") == self.FOLDER:
                        queue.append((sd["targetId"], f"{prefix}{it['name']}/"))
                        continue
                    r = self._get(f"/files/{sd.get('targetId')}", fields=self.FIELDS)
                    if r.status_code != 200:
                        continue
                    it = {**r.json(), "name": it["name"]}
                if it["mimeType"] == self.FOLDER:
                    queue.append((it["id"], f"{prefix}{it['name']}/"))
                    continue
                if it["mimeType"].startswith("application/vnd.google-apps"):
                    continue  # Docs, Sheets ... have no file content
                fp = it.get("md5Checksum") or f"{it.get('size')}-{it.get('modifiedTime')}"
                out.append({"key": f"{prefix}{it['name']}", "name": it["name"], "fp": fp,
                            "id": it["id"], "md5": it.get("md5Checksum")})
        return sorted(out, key=lambda f: f["key"])

    def _download(self, f: dict):
        for attempt in range(4):
            try:
                r = self.s.get(f"{self.API}/files/{f['id']}", params={"alt": "media", "supportsAllDrives": "true"},
                               stream=True, timeout=300)
                r.raise_for_status()
                return r
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def fetch(self, f: dict, tmp: Path) -> Path:
        dest = tmp / f["key"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            md5 = hashlib.md5()
            r = self._download(f)
            with open(dest, "wb") as out:
                for chunk in r.iter_content(1 << 20):
                    out.write(chunk)
                    md5.update(chunk)
            if not f.get("md5") or md5.hexdigest() == f["md5"]:
                return dest
            log(f"  ! checksum mismatch for {f['key']}, retrying")
        raise IOError(f"download of {f['key']} failed checksum")

    def read_text(self, f: dict) -> str:
        return self._download(f).content.decode("utf-8")


def drive_credentials(arg: str | None, cfg: dict) -> dict | None:
    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return json.loads(raw)
    path = arg or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or cfg.get("credentials")
    if path:
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            sys.exit(f"Credentials file not found: {p}")
        return json.loads(p.read_text())
    return None


# --------------------------------------------------------------------------- places

_CITIES: list[dict] | None = None


def cities_table() -> list[dict]:
    """GeoNames cities (pop > 1000) bundled with the reverse_geocoder package."""
    global _CITIES
    if _CITIES is None:
        import reverse_geocoder

        path = Path(reverse_geocoder.__file__).parent / "rg_cities1000.csv"
        with open(path, newline="", encoding="utf-8") as f:
            _CITIES = list(csv.DictReader(f))
    return _CITIES


def resolve_location(loc, where: str, default_cc: str | None = None) -> tuple[float, float] | None:
    """Accepts {lat, lon}, {city, country} or the string 'City, Country'."""
    if not loc:
        return None
    if isinstance(loc, str):
        parts = [p.strip() for p in loc.split(",")]
        loc = {"city": parts[0], "country": parts[-1] if len(parts) > 1 else default_cc}
    if not isinstance(loc, dict):
        return None
    if "lat" in loc and "lon" in loc:
        return float(loc["lat"]), float(loc["lon"])
    if loc.get("city"):
        country = loc.get("country") or default_cc
        cc = (country_from_folder(str(country)) or str(country).strip().upper()) if country else None
        hit = lookup_place(str(loc["city"]), cc)
        if hit:
            return hit
        log(f"  ! {where}: could not find city '{loc['city']}'. Use {{lat: .., lon: ..}} instead.")
    return None


JP_WARD_CITIES = {"Tokyo", "Kyoto", "Osaka"}


def normalize_place(place: dict, aliases: dict) -> tuple[str | None, str | None]:
    """GeoNames nearest place -> (city, area). Wards and suburbs fold into their city."""
    name = place.get("name") or None
    if not name:
        return None, None
    area = name
    if place.get("cc") == "KR":
        name = re.sub(r"-(si|gun)$", "", name)  # "Bucheon-si" -> "Bucheon"
    if place.get("cc") == "JP":
        name = re.sub(r"-shi$", "", name)  # "Nara-shi" -> "Nara"
        if name.endswith("-ku") and place.get("admin1") in JP_WARD_CITIES:
            name = place["admin1"]  # "Kamigyo-ku" -> "Kyoto"
    name = aliases.get(name, aliases.get(area, name))
    return name, (area if re.sub(r"-(shi|si|gun)$", "", area) != name else None)


_STATES: dict[str, list] = {}
_STATE_TOPO: dict | None = None


def state_topology(cc: str) -> dict | None:
    global _STATE_TOPO
    if _STATE_TOPO is None:
        _STATE_TOPO = json.loads(STATES_FILE.read_text(encoding="utf-8")) if STATES_FILE.exists() else {}
    return _STATE_TOPO.get(cc)


def _unwrap(ring: list) -> list:
    out = [ring[0]]
    for x, y in ring[1:]:
        px = out[-1][0]
        while x - px > 180:
            x -= 360
        while x - px < -180:
            x += 360
        out.append((x, y))
    return out


def states_for(cc: str | None) -> list[tuple[str, tuple, list, set]]:
    """[(state name, bbox, rings, names)] for a country, from photography/states.json."""
    if not cc:
        return []
    if cc not in _STATES:
        feats = []
        topo = state_topology(cc)
        if topo:
            tr = topo.get("transform")
            arcs = []
            for arc in topo["arcs"]:
                if tr:
                    (sx, sy), (tx, ty) = tr["scale"], tr["translate"]
                    x = y = 0
                    pts = []
                    for dx, dy in arc:
                        x, y = x + dx, y + dy
                        pts.append((x * sx + tx, y * sy + ty))
                else:
                    pts = [tuple(pt) for pt in arc]
                arcs.append(pts)

            def ring(idx):
                pts = []
                for i in idx:
                    a = arcs[i] if i >= 0 else arcs[~i][::-1]
                    pts.extend(a if not pts else a[1:])
                return _unwrap(pts) if pts else pts

            for obj in topo["objects"].values():
                for g in obj.get("geometries", []):
                    if g["type"] == "Polygon":
                        rings = [ring(r) for r in g["arcs"]]
                    elif g["type"] == "MultiPolygon":
                        rings = [ring(r) for poly in g["arcs"] for r in poly]
                    else:
                        continue
                    xs = [x for r in rings for x, _ in r]
                    ys = [y for r in rings for _, y in r]
                    names = {norm_name(a) for a in (g["properties"].get("a") or "").split("|") if a}
                    names.add(norm_name(g["properties"]["n"]))
                    feats.append((g["properties"]["n"], (min(xs), min(ys), max(xs), max(ys)), rings, names))
        _STATES[cc] = feats
    return _STATES[cc]


def _inside(x: float, y: float, rings: list) -> bool:
    hit = False
    for r in rings:
        j = len(r) - 1
        for i in range(len(r)):
            xi, yi = r[i]
            xj, yj = r[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
    return hit


def find_state(cc: str | None, lat: float, lon: float, hint: str | None = None) -> str | None:
    """Which state/province a point is in, using the same shapes as the map.

    `hint` is the GeoNames state of the nearest town, which is exact even where the simplified
    shapes are not (e.g. Manhattan next to New Jersey); the shapes decide when names don't match.
    """
    feats = states_for(cc)

    def containing(cands):
        for x in (lon, lon + 360, lon - 360):
            for f in cands:
                x0, y0, x1, y1 = f[1]
                if x0 <= x <= x1 and y0 <= lat <= y1 and _inside(x, lat, f[2]):
                    return f[0]
        return None

    if hint:
        h = norm_name(hint)
        exact = [f for f in feats if h in f[3]]
        if len(exact) == 1:
            return exact[0][0]
        partial = exact or [f for f in feats if any(len(a) >= 4 and (a in h or h in a) for a in f[3])]
        if len(partial) == 1:
            return partial[0][0]
        if partial and (hit := containing(partial)):
            return hit
    if hit := containing(feats):
        return hit
    best, best_d = None, 0.5 ** 2  # coastline photos: nearest state within ~50 km
    for name, (x0, y0, x1, y1), rings, _ in feats:
        if x0 - 0.5 <= lon <= x1 + 0.5 and y0 - 0.5 <= lat <= y1 + 0.5:
            for r in rings:
                for px, py in r:
                    d = (px - lon) ** 2 + (py - lat) ** 2
                    if d < best_d:
                        best, best_d = name, d
    return best


PLACE_SPLIT = re.compile(r"\s+(?:and|&|e|y|\+)\s+|\s*[,/]\s*", re.IGNORECASE)


def lookup_place(name: str, cc: str | None) -> tuple[float, float] | None:
    """Folder name -> coordinates: a city ('João Pessoa'), a state ('Paraíba'), or the first
    known one in a list ('Incheon and Seoul', 'Lisboa & Porto', 'Rio e Niterói')."""
    hit = _lookup_one(name, cc)
    if hit is None:
        for part in PLACE_SPLIT.split(name):
            if part.strip() and (hit := _lookup_one(part, cc)):
                break
    return hit


def _lookup_one(name: str, cc: str | None) -> tuple[float, float] | None:
    key = norm_name(name)
    hits = [r for r in cities_table() if norm_name(r["name"]) == key and (not cc or r["cc"] == cc)]
    if hits:  # prefer the city that also names its district/state (the capital, not a namesake village)
        hits.sort(key=lambda r: -((norm_name(r["admin2"]) == key) + (key in norm_name(r["admin1"]))))
        return float(hits[0]["lat"]), float(hits[0]["lon"])
    for _, (x0, y0, x1, y1), _, names in states_for(cc):
        if key in names:
            return (y0 + y1) / 2, (x0 + x1) / 2
    return None


def reverse_geocode(points: list[tuple[float, float]]) -> list[dict]:
    if not points:
        return []
    import reverse_geocoder

    return reverse_geocoder.search(points, mode=1, verbose=False)


# --------------------------------------------------------------------------- EXIF


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_film(film: str | None, saturation: str | None) -> str | None:
    """Fujifilm film simulation from MakerNotes (FilmMode / Saturation)."""
    if film:
        m = re.search(r"\(([^)]+)\)", film)  # "F2/Fujichrome (Velvia)" -> "Velvia"
        film = m.group(1) if m else film
        return film.replace("Pro Neg. ", "Pro Neg ").strip()
    if saturation:
        s = saturation.lower()
        if "acros" in s:
            return "Acros"
        if "sepia" in s:
            return "Sepia"
        if "b&w" in s or "monochrome" in s:
            return "Monochrome"
    return None


def clean_camera(make: str | None, model: str | None) -> str | None:
    make = (make or "").strip()
    model = (model or "").strip()
    if not model:
        return make or None
    brand = make.split()[0].capitalize() if make else ""
    if brand and brand.lower() in model.lower():
        return model
    return f"{brand} {model}".strip()


def parse_date(value, offset=None) -> str | None:
    if not value or str(value).startswith("0000"):
        return None
    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(value))
    if not m:
        return None
    s = "{}-{}-{}T{}:{}:{}".format(*m.groups())
    if offset and re.match(r"^[+-]\d{2}:\d{2}$", str(offset)):
        s += str(offset)
    return s


def normalize_exif(r: dict) -> dict:
    return {
        "date": parse_date(r.get("DateTimeOriginal") or r.get("CreateDate"), r.get("OffsetTimeOriginal")),
        "lat": _num(r.get("GPSLatitude")),
        "lon": _num(r.get("GPSLongitude")),
        "camera": clean_camera(r.get("Make"), r.get("Model")),
        "lens": (str(r.get("LensModel") or r.get("Lens") or "").strip() or None),
        "focal": _num(r.get("FocalLength")),
        "fnumber": _num(r.get("FNumber")),
        "exposure": _num(r.get("ExposureTime")),
        "iso": _num(r.get("ISO")),
        "film": clean_film(r.get("FilmMode"), r.get("Saturation")),
        "caption": (str(r.get("Title") or r.get("ImageDescription") or "").strip() or None),
    }


def exif_exiftool(files: list[Path]) -> dict[Path, dict]:
    tags = [
        "-DateTimeOriginal", "-OffsetTimeOriginal", "-CreateDate",
        "-GPSLatitude#", "-GPSLongitude#",
        "-Make", "-Model", "-LensModel", "-Lens",
        "-FocalLength#", "-FNumber#", "-ExposureTime#", "-ISO#",
        "-FilmMode", "-Saturation", "-ImageDescription", "-Title",
    ]
    out: dict[str, dict] = {}
    for i in range(0, len(files), 200):
        chunk = files[i : i + 200]
        res = subprocess.run(
            ["exiftool", "-json", "-q", "-q", "-charset", "filename=utf8", *tags, *map(str, chunk)],
            capture_output=True, text=True,
        )
        if res.stdout.strip():
            for row in json.loads(res.stdout):
                out[os.path.normpath(row["SourceFile"])] = row
    return {f: normalize_exif(out.get(os.path.normpath(str(f)), {})) for f in files}


def exif_pillow(path: Path) -> dict:
    """Fallback when exiftool is not installed (no film simulation)."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            ifd = ex.get_ifd(0x8769)
            gps = ex.get_ifd(0x8825)
    except Exception:
        return normalize_exif({})

    def dms(v, ref):
        try:
            d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -d if ref in ("S", "W") else d
        except Exception:
            return None

    lat = dms(gps.get(2), gps.get(1)) if gps else None
    lon = dms(gps.get(4), gps.get(3)) if gps else None
    return normalize_exif({
        "DateTimeOriginal": ifd.get(0x9003) or ex.get(0x0132),
        "OffsetTimeOriginal": ifd.get(0x9011),
        "GPSLatitude": lat, "GPSLongitude": lon,
        "Make": ex.get(0x010F), "Model": ex.get(0x0110),
        "LensModel": ifd.get(0xA434),
        "FocalLength": ifd.get(0x920A), "FNumber": ifd.get(0x829D),
        "ExposureTime": ifd.get(0x829A), "ISO": ifd.get(0x8827),
        "ImageDescription": ex.get(0x010E),
    })


def settings_line(e: dict) -> str | None:
    parts = []
    if e.get("focal"):
        parts.append(f"{e['focal']:g}mm")
    if e.get("fnumber"):
        parts.append(f"f/{e['fnumber']:g}")
    t = e.get("exposure")
    if t:
        parts.append(f"1/{round(1 / t)}s" if t < 0.5 else f"{t:g}s")
    if e.get("iso"):
        parts.append(f"ISO {int(e['iso'])}")
    return " · ".join(parts) or None


# --------------------------------------------------------------------------- images


def render(src: Path, full: Path, thumb: Path, cfg: dict) -> tuple[int, int, int, int]:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        big = im.copy()
        big.thumbnail((cfg["max_size"], cfg["max_size"]), Image.LANCZOS)
        full.parent.mkdir(parents=True, exist_ok=True)
        big.save(full, "WEBP", quality=cfg["quality"], method=5)  # no exif= -> metadata stripped
        small = im.copy()
        small.thumbnail((cfg["thumb_max_width"], cfg["thumb_height"]), Image.LANCZOS)
        small.save(thumb, "WEBP", quality=cfg["quality"] - 4, method=5)
        return big.width, big.height, small.width, small.height


# --------------------------------------------------------------------------- stats


def ranked(counter: Counter, labels: dict | None = None, top: int | None = None) -> list[dict]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    rows = [{"key": k, "label": (labels or {}).get(k, k), "count": c} for k, c in items]
    if top and len(rows) > top:
        rest = sum(r["count"] for r in rows[top:])
        rows = rows[:top] + [{"key": "_other", "label": f"{len(items) - top} more", "count": rest}]
    return rows


def build_stats(photos: list[dict]) -> dict:
    countries, states, cities, years, lenses, films, cameras = (Counter() for _ in range(7))
    hours = [0] * 24
    labels = {}
    for p in photos:
        if p.get("cc"):
            countries[p["cc"]] += 1
            labels[p["cc"]] = p["country"]
        if p.get("state"):
            key = f"{p['state']}|{p.get('cc')}"
            states[key] += 1
            labels[key] = f"{p['state']}, {p['country']}" if p.get("country") else p["state"]
        if p.get("city"):
            key = f"{p['city']}|{p.get('cc')}"
            cities[key] += 1
            labels[key] = f"{p['city']}, {p['country']}" if p.get("country") else p["city"]
        if p.get("date"):
            years[p["date"][:4]] += 1
            hours[int(p["date"][11:13])] += 1
        if p.get("lens"):
            lenses[p["lens"]] += 1
        if p.get("film"):
            films[p["film"]] += 1
        if p.get("camera"):
            cameras[p["camera"]] += 1
    year_rows = []
    if years:
        y0, y1 = int(min(years)), int(max(years))
        year_rows = [{"key": str(y), "label": str(y), "count": years.get(str(y), 0)} for y in range(y0, y1 + 1)]
    return {
        "countries": ranked(countries, labels),
        "states": ranked(states, labels),
        "cities": ranked(cities, labels, top=10),
        "years": year_rows,
        "hours": hours,
        "lenses": ranked(lenses, top=8),
        "films": ranked(films, top=8),
        "cameras": ranked(cameras, top=6),
    }


# --------------------------------------------------------------------------- trips


def album_parts(dirs: list[str]) -> list[str]:
    """Folders that make up the album, from the folders a photo is in.

    Country/Place/Year/...  -> [Country, Place, Year]     (João Pessoa 2026)
    Country/Year/...        -> [Country, Year]            (Japan 2025)
    Country/Place/...       -> [Country, Place]           (no year folder)
    Deeper subfolders belong to the album.
    """
    album = dirs[:1]
    for part in dirs[1:]:
        date, rest = split_date(part)
        album.append(part)
        if date:  # a year (or "2025-07 Kyoto") closes the album
            break
        if len(album) == 3:
            break
    return album


def group_trips(files: list[dict]) -> tuple[dict[str, list[dict]], dict[str, dict], list[str]]:
    trips: dict[str, list[dict]] = defaultdict(list)
    trip_yaml: dict[str, dict] = {}
    loose = []
    for f in files:
        parts = f["key"].split("/")
        is_image = Path(f["name"]).suffix.lower() in EXTS
        if len(parts) < 2:
            if is_image:
                loose.append(f["key"])
            continue
        album = "/".join(album_parts(parts[:-1]))
        if f["name"].lower() in TRIP_FILES:
            if "/".join(parts[:-1]) == album:
                trip_yaml[album] = f
        elif is_image:
            trips[album].append(f)
    return dict(trips), trip_yaml, loose


def trip_defaults(key: str) -> dict:
    """Title, year, country, place and URL slug from the album's folders."""
    parts = key.split("/")
    cc = country_from_folder(parts[0])
    country = country_name(cc) if cc else None
    place, year = None, None
    for part in parts[1:]:
        date, rest = split_date(part)
        if date:
            year = date[:4]
            place = rest or place
        else:
            place = part
    if not cc and len(parts) == 1:  # old layout: "2025-07 Kyoto" at the top
        date, rest = split_date(parts[0])
        year, place = (date[:4] if date else None), rest
    if place:
        title = place
        slug = slugify(f"{place} {year or ''}")
    else:
        title = f"{country or parts[0]} {year}" if year else (country or parts[0])
        slug = slugify(f"{country or parts[0]} {year or ''}")
    return {"cc": cc, "year": year, "place": place, "title": title, "slug": slug}


# --------------------------------------------------------------------------- main


def load_config(path: Path) -> tuple[dict, dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    cfg = {**DEFAULTS, **(raw.get("settings") or {})}
    trips = {}
    for t in raw.get("trips") or []:
        if t and "folder" in t:
            trips[str(t["folder"]).strip("/")] = t
    return cfg, trips


def load_manifest(cfg: dict) -> dict:
    try:
        m = json.loads(MANIFEST.read_text())
        if m.get("version") == MANIFEST_VERSION and m.get("render") == {k: cfg[k] for k in RENDER_KEYS}:
            return m.get("files", {})
    except (OSError, ValueError):
        pass
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "trips.yaml"))
    ap.add_argument("--source", help="local folder instead of Google Drive")
    ap.add_argument("--drive", help="Drive folder ID (overrides trips.yaml)")
    ap.add_argument("--credentials", help="service account JSON file")
    ap.add_argument("--force", action="store_true", help="re-download and re-encode every photo")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--ci", action="store_true", help="exit quietly if Drive is not configured yet")
    args = ap.parse_args()

    cfg, trip_cfg = load_config(Path(args.config))
    aliases = {str(k): str(v) for k, v in (cfg.get("place_aliases") or {}).items()}

    folder_id = args.drive or os.environ.get("GDRIVE_FOLDER_ID") or cfg.get("drive_folder_id")
    if args.source or (cfg.get("source") and not folder_id):
        source = LocalSource(Path(os.path.expanduser(args.source or cfg["source"])).resolve())
    elif folder_id:
        creds = drive_credentials(args.credentials, cfg)
        if not creds and args.ci:
            log("::warning::GDRIVE_SERVICE_ACCOUNT_JSON secret is not set; keeping the cached photos.")
            return
        if not creds:
            sys.exit("No Drive credentials. Set GDRIVE_SERVICE_ACCOUNT_JSON or pass --credentials FILE "
                     "(or use --source for a local folder).")
        source = DriveSource(str(folder_id), creds)
    elif args.ci:
        log("::warning::drive_folder_id is empty in photography/trips.yaml; keeping the cached photos.")
        return
    else:
        sys.exit("Nothing to read: set drive_folder_id in photography/trips.yaml or pass --source FOLDER.")

    use_exiftool = shutil.which("exiftool") is not None
    log(f"Source: {source.describe()}")
    log(f"EXIF reader: {'exiftool' if use_exiftool else 'Pillow (install exiftool to get film simulations)'}")
    if not HEIC:
        log("HEIC support: off (pip install pillow-heif to include .heic photos)")

    groups, folder_yaml, loose = group_trips(source.walk())
    for k in loose:
        log(f"  ! {k}: photos must be inside Country/Place/Year folders, skipped")
    for k in trip_cfg:
        if k not in groups:
            log(f"  ! trips.yaml lists '{k}' but there is no such trip folder")

    manifest = {} if args.force else load_manifest(cfg)
    new_manifest: dict[str, dict] = {}
    render_cfg = {k: cfg[k] for k in RENDER_KEYS}
    trips, photos, used_slugs = [], [], set()
    no_location: list[str] = []
    counts = Counter()
    keep: set[Path] = set()
    workers = max(1, os.cpu_count() or 2)

    for key in sorted(groups):
        d = trip_defaults(key)
        tc = dict(trip_cfg.get(key, {}))
        if key in folder_yaml:  # trip.yaml inside the trip folder wins over the repo
            try:
                tc.update(yaml.safe_load(source.read_text(folder_yaml[key])) or {})
            except Exception as e:  # a typo in trip.yaml should not break the site
                log(f"  ! {key}/trip.yaml could not be read: {e}")
        if tc.get("hidden"):
            continue
        slug = slugify(tc.get("slug") or d["slug"])
        base_slug, n = slug, 2
        while slug in used_slugs:
            slug, n = f"{base_slug}-{n}", n + 1
        used_slugs.add(slug)
        trip_cc = country_from_folder(str(tc["country"])) if tc.get("country") else d["cc"]

        overrides = {str(k): v or {} for k, v in (tc.get("photos") or {}).items()}
        fallback = resolve_location(tc.get("location"), key, trip_cc)
        if not fallback and not tc.get("location") and d["place"]:
            fallback = lookup_place(d["place"], trip_cc)
            if not fallback:
                log(f"  ! '{d['place']}' is not a known city or state; add `location:` to its trip.yaml "
                    "so photos without GPS appear on the map")
        tfiles = groups[key]
        log(f"\n{key}  ->  /photography/{slug}/  ({len(tfiles)} photos)")

        # which photos are new or changed since the last run?
        entries, todo, ids = [], [], set()
        for f in tfiles:
            pid = slugify(Path(f["name"]).stem)
            base_pid, n = pid, 2
            while pid in ids:
                pid, n = f"{base_pid}-{n}", n + 1
            ids.add(pid)
            full, thumb = OUT_IMG / slug / f"{pid}.webp", OUT_IMG / slug / f"{pid}-t.webp"
            keep.update({full, thumb})
            m = manifest.get(f["key"])
            ok = bool(m and m["fp"] == f["fp"] and m["out"] == f"{slug}/{pid}" and full.exists() and thumb.exists())
            entry = {"f": f, "pid": pid, "full": full, "thumb": thumb, "rec": m if ok else None}
            entries.append(entry)
            if not ok:
                todo.append(entry)
        counts["unchanged"] += len(entries) - len(todo)

        if todo and not args.dry_run:
            with tempfile.TemporaryDirectory(prefix="photos-") as tmpdir:
                tmp = Path(tmpdir)
                if isinstance(source, DriveSource):
                    log(f"  downloading {len(todo)} ...")
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futs = {pool.submit(source.fetch, e["f"], tmp): e for e in todo}
                    for fut in as_completed(futs):
                        try:
                            futs[fut]["path"] = fut.result()
                        except Exception as ex:
                            log(f"  ! could not download {futs[fut]['f']['key']}: {ex}")
                ready = [e for e in todo if e.get("path")]
                paths = [e["path"] for e in ready]
                exif = exif_exiftool(paths) if use_exiftool else {p: exif_pillow(p) for p in paths}
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(render, e["path"], e["full"], e["thumb"], render_cfg): e for e in ready}
                    for fut in as_completed(futs):
                        e = futs[fut]
                        try:
                            w, h, tw, th = fut.result()
                        except Exception as ex:
                            log(f"  ! could not read {e['f']['key']}: {ex}")
                            continue
                        e["rec"] = {"fp": e["f"]["fp"], "out": f"{slug}/{e['pid']}", "exif": exif[e["path"]],
                                    "w": w, "h": h, "tw": tw, "th": th}
                        counts["encoded"] += 1
            log(f"  encoded {sum(1 for e in todo if e['rec'])} new or changed")

        trip_photos, sub_places = [], {}
        for e in entries:
            rec = e["rec"]
            if rec is None:
                if not args.dry_run:
                    continue  # this photo failed: skip it, keep the rest of the site
                rec = {"exif": normalize_exif({}), "w": 0, "h": 0, "tw": 0, "th": 0}
            else:
                new_manifest[e["f"]["key"]] = rec
            rel = e["f"]["key"][len(key) + 1:]
            ov = overrides.get(rel) or overrides.get(e["f"]["name"]) or {}
            if ov.get("hidden"):
                continue
            ex = dict(rec["exif"])
            for k in ("date", "camera", "lens", "film", "caption"):
                if ov.get(k):
                    ex[k] = str(ov[k])

            # location priority: per-photo override > GPS in the file > trip fallback
            loc, loc_src = resolve_location(ov.get("location"), f"{key}/{rel}", trip_cc), "manual"
            if not loc and ex["lat"] is not None and ex["lon"] is not None and not (ex["lat"] == 0 and ex["lon"] == 0):
                loc, loc_src = (ex["lat"], ex["lon"]), "gps"
            if not loc and "/" in rel:  # a subfolder named after a city: .../2026/Seoul/x.jpg
                sub = rel.rsplit("/", 1)[0].split("/")[-1]
                if sub not in sub_places:
                    sub_places[sub] = lookup_place(sub, trip_cc)
                if sub_places[sub]:
                    loc, loc_src = sub_places[sub], "folder"
            if not loc and fallback:
                loc, loc_src = fallback, "trip"
            if not loc and not tc.get("hide_on_map"):
                no_location.append(e["f"]["key"])

            dec = cfg["coord_decimals"]
            trip_photos.append({
                "id": f"{slug}/{e['pid']}", "trip": slug, "file": rel,
                "src": f"{slug}/{e['pid']}.webp", "thumb": f"{slug}/{e['pid']}-t.webp",
                "w": rec["w"], "h": rec["h"], "tw": rec["tw"], "th": rec["th"],
                "date": ex["date"],
                "lat": round(loc[0], dec) if loc else None,
                "lon": round(loc[1], dec) if loc else None,
                "loc_src": loc_src if loc else None,
                "camera": ex["camera"], "lens": ex["lens"], "settings": settings_line(ex),
                "film": ex["film"], "caption": ex["caption"],
                "cc": None, "country": None, "state": None, "city": None, "area": None,
                "_place": ov.get("place"),
            })
        if not trip_photos:
            continue

        # places, offline; photos without GPS still count for their folder's country
        located = [p for p in trip_photos if p["lat"] is not None]
        for p, place in zip(located, reverse_geocode([(p["lat"], p["lon"]) for p in located])):
            p["cc"] = place.get("cc")
            p["city"], p["area"] = normalize_place(place, aliases)
            if d["place"] and p["city"] and norm_name(p["city"]) == norm_name(d["place"]):
                p["city"] = d["place"]  # GeoNames names are ASCII: "Joao Pessoa" -> "João Pessoa"
            p["_admin1"] = place.get("admin1")
        for p in trip_photos:
            p["cc"] = p["cc"] or trip_cc
            p["country"] = country_name(p["cc"])
            admin1 = p.pop("_admin1", None)
            if p["lat"] is not None:
                p["state"] = find_state(p["cc"], p["lat"], p["lon"], admin1)
            place_override = p.pop("_place")
            if place_override:  # e.g. "Fushimi Inari", shown in the caption
                p["area"] = str(place_override)
            if tc.get("hide_on_map"):
                p["lat"] = p["lon"] = None

        trip_photos.sort(key=lambda p: (p["date"] or "9999", p["file"]))
        dates = [p["date"] for p in trip_photos if p["date"]]
        cover_name = str(tc.get("cover") or "")
        cover = next((p for p in trip_photos if cover_name and cover_name in (p["file"], Path(p["file"]).name)), None)
        cover = cover or trip_photos[0]
        cc_counts = Counter(p["cc"] for p in trip_photos if p.get("cc"))
        city_counts = Counter(p["city"] for p in trip_photos if p.get("city"))
        state_counts = Counter(p["state"] for p in trip_photos if p.get("state"))
        title = str(tc.get("title") or d["title"])
        countries_ = [country_name(cc) for cc, _ in cc_counts.most_common()]
        top_state = state_counts.most_common(1)[0][0] if state_counts else None
        states_ = [top_state] if top_state and norm_name(top_state) not in norm_name(title) else []
        if title in countries_ or title.startswith(tuple(f"{c} " for c in countries_)):
            subtitle = ", ".join(c for c, _ in city_counts.most_common(2))  # "Japan 2025" -> "Kyoto, Tokyo"
        elif len(countries_) == 1:
            subtitle = ", ".join(states_ + countries_)  # "João Pessoa" -> "Paraíba, Brazil"; "Incheon" -> "South Korea"
        else:
            subtitle = ", ".join(countries_)
        start = str(tc["date"]) if tc.get("date") else (min(dates)[:10] if dates else None)
        trips.append({
            "slug": slug, "folder": key,
            "title": title,
            "subtitle": subtitle,
            "description": str(tc.get("description") or "").strip(),
            "start": start,
            "end": max(dates)[:10] if dates else start,
            "year": d["year"] or (start or "")[:4] or None,
            "count": len(trip_photos),
            "cover": {k: cover[k] for k in ("id", "src", "thumb", "w", "h", "tw", "th")},
            "countries": [{"cc": cc, "name": country_name(cc)} for cc, _ in cc_counts.most_common()],
            "cities": [c for c, _ in city_counts.most_common(6)],
            "states": [st for st, _ in state_counts.most_common()],
        })
        for p in trip_photos:
            p.pop("file", None)
        photos.extend(trip_photos)

    trips.sort(key=lambda t: (t["start"] or t["year"] or "0000", t["slug"]), reverse=True)
    order = {t["slug"]: i for i, t in enumerate(trips)}
    photos.sort(key=lambda p: (order[p["trip"]], p["date"] or "9999"))

    countries = {p["cc"] for p in photos if p.get("cc")}
    cities = {(p["city"], p.get("cc")) for p in photos if p.get("city")}
    states = {(p["state"], p.get("cc")) for p in photos if p.get("state")}
    years = sorted({p["date"][:4] for p in photos if p.get("date")})
    data = {
        "totals": {
            "photos": len(photos),
            "geotagged": sum(1 for p in photos if p["lat"] is not None),
            "countries": len(countries),
            "states": len(states),
            "cities": len(cities),
            "trips": len(trips),
            "first_year": years[0] if years else None,
            "last_year": years[-1] if years else None,
        },
        "trips": trips,
        "stats": build_stats(photos),
        "photos": photos,
    }

    # report
    log("\n" + "-" * 60)
    t = data["totals"]
    log(f"{t['photos']} photos, {t['trips']} albums, {t['countries']} countries, {t['states']} states, "
        f"{t['cities']} places")
    src_count = Counter(p["loc_src"] for p in photos)
    log(f"Map locations: {src_count.get('gps', 0)} from GPS, {src_count.get('folder', 0)} from city subfolders, "
        f"{src_count.get('trip', 0)} from the album's place, {src_count.get('manual', 0)} manual, "
        f"{len(no_location)} without location")
    if no_location:
        log("\nNot on the map (no GPS). Add `location: City` to that trip's trip.yaml to place them:")
        for k in no_location[:40]:
            log(f"  - {k}")
        if len(no_location) > 40:
            log(f"  ... and {len(no_location) - 40} more")

    if args.dry_run:
        new = sum(1 for p in photos if not p["w"])
        if new:
            log(f"\nDry run: {new} new photos were not downloaded, so their GPS, dates and lenses are not "
                "known yet (the counts above treat them as having no GPS). Nothing written.")
        else:
            log("\nDry run: nothing written.")
        return

    removed = 0
    if OUT_IMG.exists():
        for f in OUT_IMG.rglob("*.webp"):
            if f not in keep:
                f.unlink()
                removed += 1
        for dpath in sorted(OUT_IMG.rglob("*"), reverse=True):
            if dpath.is_dir() and not any(dpath.iterdir()):
                dpath.rmdir()
    # state shapes for the map, only for countries with photos
    if OUT_STATES.exists():
        shutil.rmtree(OUT_STATES)
    OUT_STATES.mkdir(parents=True)
    for cc in sorted(countries):
        if topo := state_topology(cc):
            (OUT_STATES / f"{cc}.json").write_text(json.dumps(topo, separators=(",", ":")), encoding="utf-8")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    CACHE_DIR.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps({"version": MANIFEST_VERSION, "render": render_cfg, "files": new_manifest},
                                   ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    log(f"\nImages: {counts['encoded']} new or changed, {counts['unchanged']} unchanged, {removed} removed")
    log(f"Wrote {OUT_JSON.relative_to(REPO)}")


if __name__ == "__main__":
    main()
