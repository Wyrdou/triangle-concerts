#!/usr/bin/env python3
"""Scanner de concerts du triangle Lyon, Saint-Étienne, Clermont-Ferrand.

Interroge Ticketmaster, OpenAgenda et l'agenda ouvert de Clermont-Ferrand, garde
les concerts de la zone qui correspondent aux artistes et styles de
docs/config.json, publie les résultats pour la page web et envoie sur Telegram
uniquement ce qui a changé depuis le scan précédent.
Aucune dépendance : la bibliothèque standard de Python suffit.
"""
from __future__ import annotations

import copy
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs" / "config.json"
STYLES_PATH = ROOT / "docs" / "styles.json"
CONCERTS_PATH = ROOT / "docs" / "data" / "concerts.json"
STATUS_PATH = ROOT / "docs" / "data" / "status.json"
STATE_PATH = ROOT / "state" / "state.json"

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
OPENAGENDA_URL = "https://api.openagenda.com/v2"
OPENAGENDA_PAGE_SIZE = 300
OPENAGENDA_MAX_PAGES = 6
OPENAGENDA_MAX_AGENDAS = 25
OPENAGENDA_REFRESH_DAYS = 7
OPENAGENDA_SEARCHES = ["Lyon concert", "Saint-Étienne concert", "Clermont-Ferrand concert",
                       "Lyon", "Saint-Étienne", "Clermont-Ferrand"]
OPEN_LICENSES = ("etalab", "cc0", "cc-by", "odc-by", "ogl")
RESTRICTED_LICENSES = ("nc", "nd")
CLERMONT_URL = "https://www.data.gouv.fr/api/1/datasets/r/ad56f730-1383-4507-8f10-efea7e26b814"
CLERMONT_PAGE = "https://www.data.gouv.fr/datasets/agenda-ville-de-clermont-ferrand"
CLERMONT_CREDIT = "Clermont Auvergne Métropole, Clermont Auvergne Volcans, Département du Puy-de-Dôme"
LICENCE_OUVERTE_URL = "https://www.etalab.gouv.fr/licence-ouverte-open-licence"
CONCERT_WORDS = ("concert", "concerts", "recital", "showcase", "dj set", "live", "festival de musique",
                 "musiques actuelles")
NOT_CONCERT_WORDS = ("atelier", "stage", "cours", "exposition", "conference", "visite", "initiation",
                     "cine concert", "bal", "scene ouverte")
MAX_CONCERT_TIMINGS = 12  # Au-delà, c'est une exposition ou un cycle, pas un concert.
SOURCE_ORDER = {"ticketmaster": 0, "openagenda": 1, "clermont": 2}
OA_STATUS = {2: "rescheduled", 4: "postponed", 5: "complet", 6: "canceled"}
MUSIC_SEGMENT_ID = "KZFzniwnSyZfZ7v7nJ"
USER_AGENT = "TriangleConcerts/1.0 (scanner personnel de concerts)"
PAGE_SIZE = 200
MAX_RESULTS = 1000  # L'API ne pagine pas au-delà du 1000e résultat.
REQUEST_PAUSE = 0.25  # L'API accepte 5 requêtes par seconde.
WINDOW_DAYS = 90
WINDOW_COUNT = 6  # Environ 18 mois devant nous.
CANCELLED_STATUSES = {"canceled", "postponed", "rescheduled"}
IGNORED_GENRES = {"undefined", "other", "music", "musique", "autre", "autres"}
MAX_ALERTS_PER_SECTION = 12
TELEGRAM_LIMIT = 3800
FAILURES_BEFORE_ALERT = 3
PARIS = ZoneInfo("Europe/Paris")
MONTHS = ["janv.", "févr.", "mars", "avril", "mai", "juin", "juil.", "août",
          "sept.", "oct.", "nov.", "déc."]
DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


class ScanError(Exception):
    """Erreur affichable publiquement : ne contient jamais de clé."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# --- Texte ------------------------------------------------------------------

def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Hip-Hop/Rap » -> « hip hop rap »."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()


def contains_phrase(haystack: str, needle: str) -> bool:
    """Cherche des mots entiers : « muse » ne correspond pas à « museum »."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def fr_date(value: datetime | None) -> str:
    if value is None:
        return "date à confirmer"
    local = value.astimezone(PARIS)
    return f"{DAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} {local.year}"


def fr_datetime(value: datetime) -> str:
    local = value.astimezone(PARIS)
    return f"{fr_date(value)} à {local.hour} h {local.minute:02d}"


def fr_local_date(iso_date: str | None) -> str:
    if not iso_date:
        return "date à confirmer"
    return fr_date(datetime.fromisoformat(iso_date).replace(tzinfo=PARIS))


def parse_datetime(value: str | None) -> datetime | None:
    """Ticketmaster met parfois 1900 comme date fictive : on l'ignore."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed if parsed.year >= 2000 else None


# --- Géographie -------------------------------------------------------------

def local_km(lat: float, lon: float, origin_lat: float) -> tuple[float, float]:
    return lon * 111.32 * math.cos(math.radians(origin_lat)), lat * 110.57


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = local_km(a[0], a[1], a[0])
    bx, by = local_km(b[0], b[1], a[0])
    return math.hypot(ax - bx, ay - by)


def distance_to_segment(p, a, b) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    ratio = 0 if length == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy))


def distance_to_triangle(lat: float, lon: float, corners: list[dict]) -> float:
    """0 dans le triangle, sinon distance en km jusqu'au bord le plus proche."""
    p = local_km(lat, lon, lat)
    points = [local_km(c["lat"], c["lon"], lat) for c in corners]
    signs = []
    for i in range(3):
        a, b = points[i], points[(i + 1) % 3]
        signs.append((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
    if all(s >= 0 for s in signs) or all(s <= 0 for s in signs):
        return 0.0
    return min(distance_to_segment(p, points[i], points[(i + 1) % 3]) for i in range(3))


def bounding_circle(corners: list[dict], margin_km: float) -> tuple[float, float, int]:
    lats = [c["lat"] for c in corners]
    lons = [c["lon"] for c in corners]
    lat, lon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    radius = max(distance_km((lat, lon), (c["lat"], c["lon"])) for c in corners)
    return lat, lon, math.ceil(radius + margin_km + 5)


def geohash(lat: float, lon: float, precision: int = 9) -> str:
    alphabet = "0123456789bcdefghjkmnpqrstuvwxyz"
    ranges = [[-90.0, 90.0], [-180.0, 180.0]]
    value, bits, use_lon, output = 0, 0, True, ""
    while len(output) < precision:
        target, bounds = (lon, ranges[1]) if use_lon else (lat, ranges[0])
        middle = (bounds[0] + bounds[1]) / 2
        value <<= 1
        if target >= middle:
            value |= 1
            bounds[0] = middle
        else:
            bounds[1] = middle
        use_lon = not use_lon
        bits += 1
        if bits == 5:
            output += alphabet[value]
            value, bits = 0, 0
    return output


# --- Ticketmaster -----------------------------------------------------------

def api_get(params: dict, key: str) -> dict:
    url = f"{API_URL}?{urllib.parse.urlencode({**params, 'apikey': key})}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt + 1)
                continue
            hint = " : vérifiez la clé TICKETMASTER_API_KEY" if exc.code in (401, 403) else ""
            raise ScanError(f"Ticketmaster a répondu {exc.code}{hint}.") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 4:
                time.sleep(2 ** attempt + 1)
                continue
            raise ScanError("Ticketmaster est injoignable ou a envoyé une réponse illisible.") from None
    raise ScanError("Ticketmaster n'a pas répondu.")


def api_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def search(base: dict, key: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Récupère toutes les pages, en coupant la période si elle dépasse 1000 résultats."""
    params = dict(base)
    if start and end:
        params.update(startDateTime=api_date(start), endDateTime=api_date(end))
    events: list[dict] = []
    page = 0
    while True:
        data = api_get({**params, "size": PAGE_SIZE, "page": page}, key)
        info = data.get("page", {})
        too_many = info.get("totalElements", 0) > MAX_RESULTS
        if page == 0 and too_many and start and end and end - start > timedelta(days=2):
            middle = start + (end - start) / 2
            return search(base, key, start, middle) + search(base, key, middle, end)
        events.extend(data.get("_embedded", {}).get("events", []))
        page += 1
        if page >= info.get("totalPages", 0) or page * PAGE_SIZE >= MAX_RESULTS:
            return events
        time.sleep(REQUEST_PAUSE)


def fetch_json(url: str, headers: dict | None = None, label: str = "La source"):
    # Certains pare-feu refusent l'identité par défaut de Python avec un 403.
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt + 1)
                continue
            raise ScanError(f"{label} a répondu {exc.code}.", exc.code) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(2 ** attempt + 1)
                continue
            raise ScanError(f"{label} est injoignable ou a envoyé une réponse illisible.") from None
    raise ScanError(f"{label} n'a pas répondu.")


def choose_geo_filter(common: dict, key: str, lat: float, lon: float) -> tuple[dict, int]:
    """geoPoint remplace latlong, déprécié. latlong reste une solution de repli."""
    options = [{"geoPoint": geohash(lat, lon)}, {"latlong": f"{lat:.4f},{lon:.4f}"}]
    for option in options:
        data = api_get({**common, **option, "segmentId": MUSIC_SEGMENT_ID, "size": 1}, key)
        total = data.get("page", {}).get("totalElements", 0)
        if total:
            return option, total
        time.sleep(REQUEST_PAUSE)
    return options[0], 0


def fetch_events(config: dict, key: str, now: datetime) -> tuple[list[dict], int]:
    zone = config["zone"]
    lat, lon, radius = bounding_circle(zone["coins"], zone.get("marge_km", 25))
    common = {"radius": radius, "unit": "km", "countryCode": "FR", "locale": "*", "sort": "date,asc"}
    geo, probe_total = choose_geo_filter(common, key, lat, lon)
    base = {**common, **geo}

    events: list[dict] = []
    for index in range(WINDOW_COUNT):
        start = now + timedelta(days=WINDOW_DAYS * index)
        end = start + timedelta(days=WINDOW_DAYS)
        events += search({**base, "segmentId": MUSIC_SEGMENT_ID}, key, start, end)
        time.sleep(REQUEST_PAUSE)
    # Recherche par nom sans filtre « musique » : rattrape les concerts mal classés.
    for artist in config.get("artistes", []):
        events += search({**base, "keyword": artist}, key)
        time.sleep(REQUEST_PAUSE)
    return events, probe_total


def classification_names(raw: dict) -> list[str]:
    sources = list(raw.get("classifications") or [])
    for attraction in raw.get("_embedded", {}).get("attractions", []) or []:
        sources += attraction.get("classifications") or []
    names: list[str] = []
    for item in sources:
        for level in ("genre", "subGenre"):
            name = (item.get(level) or {}).get("name", "").strip()
            if name and normalize(name) not in IGNORED_GENRES and name not in names:
                names.append(name)
    return names


def parse_sales(raw: dict) -> list[dict]:
    sales = []
    public = (raw.get("sales") or {}).get("public") or {}
    start = None if public.get("startTBD") else parse_datetime(public.get("startDateTime"))
    if start:
        sales.append({"type": "publique", "nom": "Vente générale", "debut": api_date(start)})
    for presale in (raw.get("sales") or {}).get("presales") or []:
        start = parse_datetime(presale.get("startDateTime"))
        if start:
            sales.append({"type": "prevente", "nom": presale.get("name") or "Prévente", "debut": api_date(start)})
    return sorted(sales, key=lambda sale: sale["debut"])


def parse_event(raw: dict) -> dict:
    venues = raw.get("_embedded", {}).get("venues") or [{}]
    venue = venues[0]
    place = raw.get("place") or {}
    location = venue.get("location") or place.get("location") or {}
    start = (raw.get("dates") or {}).get("start") or {}
    artists = [a.get("name", "") for a in raw.get("_embedded", {}).get("attractions", []) or [] if a.get("name")]
    try:
        lat, lon = float(location["latitude"]), float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        lat, lon = None, None
    url = raw.get("url") or ""
    return {
        "id": raw["id"],
        "ids": [raw["id"]],
        "titre": raw.get("name") or (artists[0] if artists else "Concert"),
        "artistes": artists,
        "date": start.get("localDate"),
        "heure": (start.get("localTime") or "")[:5] if isinstance(start.get("localTime"), str) else "",
        "salle": venue.get("name") or place.get("name") or "Salle à confirmer",
        "salle_id": venue.get("id") or "",
        "ville": (venue.get("city") or place.get("city") or {}).get("name") or "",
        "lat": lat,
        "lon": lon,
        "url": url if url.startswith("https://") else "",
        "styles": classification_names(raw),
        "statut": ((raw.get("dates") or {}).get("status") or {}).get("code") or "",
        "ventes": parse_sales(raw),
        "source": {"type": "ticketmaster", "nom": "Ticketmaster"},
    }


def merge_duplicates(concerts: list[dict]) -> list[dict]:
    """Regroupe un concert publié plusieurs fois (carré or, offre VIP, etc.)."""
    groups: dict[tuple, dict] = {}
    for concert in sorted(concerts, key=lambda c: c["id"]):
        who = normalize(concert["artistes"][0] if concert["artistes"] else concert["titre"])
        where = concert["salle_id"] or normalize(concert["salle"])
        group_key = (concert["date"], where, who)
        kept = groups.get(group_key)
        if kept is None:
            groups[group_key] = concert
            continue
        if concert["id"] not in kept["ids"]:
            kept["ids"].append(concert["id"])
        kept["styles"] += [s for s in concert["styles"] if s not in kept["styles"]]
        known = {(s["nom"], s["debut"]) for s in kept["ventes"]}
        kept["ventes"] += [s for s in concert["ventes"] if (s["nom"], s["debut"]) not in known]
        kept["ventes"].sort(key=lambda sale: sale["debut"])
    return list(groups.values())


def same_concert(first: dict, second: dict) -> bool:
    """Reconnaît un même concert publié par deux sources différentes."""
    if not first["date"] or first["date"] != second["date"]:
        return False
    if None not in (first["lat"], second["lat"]):
        near = distance_km((first["lat"], first["lon"]), (second["lat"], second["lon"])) < 3
    else:
        near = normalize(first["ville"]) == normalize(second["ville"]) != ""
    if not near:
        return False
    names = {normalize(n) for concert in (first, second) for n in [concert["titre"], *concert["artistes"]]}
    words_a = set(normalize(first["titre"]).split())
    words_b = set(normalize(second["titre"]).split())
    overlap = len(words_a & words_b) / max(1, len(words_a | words_b))
    title_b = normalize(second["titre"])
    title_a = normalize(first["titre"])
    contained = any(name and len(name) > 3 and (contains_phrase(title_a, name) and contains_phrase(title_b, name))
                    for name in names)
    return overlap >= 0.5 or contained


def merge_sources(concerts: list[dict]) -> list[dict]:
    """Garde la fiche la plus riche : Ticketmaster, seule à donner les dates de vente."""
    kept: list[dict] = []
    for concert in sorted(concerts, key=lambda c: SOURCE_ORDER.get(c["source"]["type"], 9)):
        twin = next((other for other in kept if same_concert(other, concert)), None)
        if twin is None:
            kept.append(concert)
        else:
            twin["ids"] = sorted(set(twin["ids"]) | set(concert["ids"]))
    return kept


# --- OpenAgenda et agenda de Clermont-Ferrand ---------------------------------

def looks_like_concert(*texts: str) -> bool:
    text = normalize(" ".join(texts))
    return any(contains_phrase(text, word) for word in CONCERT_WORDS)


def is_excluded_format(title: str) -> bool:
    text = normalize(title)
    return any(contains_phrase(text, word) for word in NOT_CONCERT_WORDS)


def query_string(params: list[tuple[str, object]]) -> str:
    return urllib.parse.urlencode(params)


def oa_get(path: str, params: list, key: str) -> dict:
    url = f"{OPENAGENDA_URL}{path}?{query_string(params)}"
    return fetch_json(url, {"key": key}, "OpenAgenda")


def discover_agendas(key: str, searches: list[str], limit: int = OPENAGENDA_MAX_AGENDAS) -> list[dict]:
    found: dict[int, str] = {}
    for search_text in searches:
        data = oa_get("/agendas", [("search", search_text), ("size", 20)], key)
        for agenda in data.get("agendas", []):
            if agenda.get("uid") and len(found) < limit:
                found.setdefault(agenda["uid"], agenda.get("title") or str(agenda["uid"]))
        time.sleep(REQUEST_PAUSE)
    return [{"uid": uid, "titre": title} for uid, title in found.items()]


def choose_agendas(options: dict, key: str, state: dict, now: datetime) -> list[dict]:
    """Les agendas choisis dans les réglages priment sur la recherche automatique."""
    manual = [{"uid": int(uid), "titre": str(uid)} for uid in options.get("agendas", [])]
    if manual:
        return manual
    cache = state.get("openagenda", {})
    fresh = parse_datetime(cache.get("date"))
    if fresh and now - fresh < timedelta(days=OPENAGENDA_REFRESH_DAYS) and cache.get("agendas"):
        return cache["agendas"]
    try:
        agendas = discover_agendas(key, options.get("recherches") or OPENAGENDA_SEARCHES)
    except ScanError as exc:
        if exc.code in (401, 403):
            raise ScanError(f"OpenAgenda refuse la recherche d'agendas ({exc.code}) : vérifiez que la clé "
                            "autorise la lecture.", exc.code) from None
        raise
    state["openagenda"] = {"date": api_date(now), "agendas": agendas}
    return agendas


def license_is_open(rights: dict) -> bool:
    license_id = ((rights or {}).get("license") or {}).get("id", "").lower()
    parts = license_id.replace(".", "-").split("-")
    return license_id.startswith(OPEN_LICENSES) and not any(p in parts for p in RESTRICTED_LICENSES)


def next_timing(timings: list[dict], now: datetime) -> datetime | None:
    starts = [parse_oa_date(t.get("begin")) for t in timings or []]
    upcoming = sorted(start for start in starts if start and start >= now)
    return upcoming[0] if upcoming else None


def parse_oa_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return parse_datetime(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value))


def parse_openagenda_event(raw: dict, now: datetime) -> dict | None:
    """Renvoie un concert, ou None si l'événement n'en est pas un."""
    title = raw.get("title") or ""
    if isinstance(title, dict):
        title = title.get("fr") or next(iter(title.values()), "")
    description = raw.get("description") or ""
    if isinstance(description, dict):
        description = description.get("fr") or next(iter(description.values()), "")
    keywords = raw.get("keywords") or []
    if isinstance(keywords, dict):
        keywords = keywords.get("fr") or next(iter(keywords.values()), [])
    timings = raw.get("timings") or []
    start = next_timing(timings, now)
    if not title or start is None or len(timings) > MAX_CONCERT_TIMINGS or raw.get("attendanceMode") == 2:
        return None
    if is_excluded_format(title) or not looks_like_concert(title, " ".join(keywords), description):
        return None
    location = raw.get("location") or {}
    rights = raw.get("rights") or {}
    licensor = rights.get("licensor") or {}
    license_info = rights.get("license") or {}
    local = start.astimezone(PARIS)
    url = raw.get("canonicalUrl") or ""
    try:
        lat, lon = float(location["latitude"]), float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        lat, lon = None, None
    return {
        "id": f"oa-{raw['uid']}",
        "ids": [f"oa-{raw['uid']}"],
        "titre": title,
        "artistes": [],
        "date": local.date().isoformat(),
        "heure": local.strftime("%H:%M"),
        "salle": location.get("name") or "Lieu à confirmer",
        "salle_id": f"oa-{location.get('uid', '')}",
        "ville": location.get("adminLevel4") or location.get("city") or "",
        "lat": lat,
        "lon": lon,
        "url": url if url.startswith("https://") else "",
        "styles": [k for k in keywords if normalize(k) not in ("concert", "concerts", "musique", "gratuit")],
        "statut": OA_STATUS.get(raw.get("status"), ""),
        "ventes": [],
        "_texte": normalize(f"{description} {' '.join(keywords)}"),
        "source": {
            "type": "openagenda",
            "nom": licensor.get("name") or "OpenAgenda",
            "url": licensor.get("url") or "",
            "licence": license_info.get("name") or "",
            "licence_url": license_info.get("url") or "",
            "maj": (raw.get("updatedAt") or "")[:10],
        },
    }


def fetch_openagenda(config: dict, key: str, state: dict, now: datetime) -> tuple[list[dict], dict]:
    zone = config["zone"]
    margin = zone.get("marge_km", 25)
    lats = [c["lat"] for c in zone["coins"]]
    lons = [c["lon"] for c in zone["coins"]]
    dlat = margin / 110.57
    dlon = margin / (111.32 * math.cos(math.radians(max(lats))))
    box = [("geo[northEast][lat]", round(max(lats) + dlat, 4)), ("geo[northEast][lng]", round(max(lons) + dlon, 4)),
           ("geo[southWest][lat]", round(min(lats) - dlat, 4)), ("geo[southWest][lng]", round(min(lons) - dlon, 4))]
    options = (config.get("sources") or {}).get("openagenda") or {}
    agendas = choose_agendas(options, key, state, now)
    concerts: dict[str, dict] = {}
    refused = 0
    truncated = 0
    per_agenda = []
    errors = []
    for agenda in agendas:
        params = [("relative[]", "current"), ("relative[]", "upcoming"), ("monolingual", "fr"), ("detailed", 1),
                  ("sort", "timings.asc"), ("size", OPENAGENDA_PAGE_SIZE), *box]
        after: list = []
        found = 0
        failure = ""
        try:
            for page in range(OPENAGENDA_MAX_PAGES):
                data = oa_get(f"/agendas/{agenda['uid']}/events", params + [("after[]", value) for value in after], key)
                for raw in data.get("events", []):
                    if not raw.get("uid") or not (concert := parse_openagenda_event(raw, now)):
                        continue
                    if not license_is_open(raw.get("rights")):
                        refused += 1
                        continue
                    found += concert["id"] not in concerts
                    concerts[concert["id"]] = concert
                after = data.get("after") or []
                time.sleep(REQUEST_PAUSE)
                if not after:
                    break
            else:
                truncated += 1
        except ScanError as exc:  # Un agenda privé ne doit pas bloquer les autres.
            errors.append(exc)
            failure = str(exc.code or "réseau")
        entry = {"uid": agenda["uid"], "titre": agenda["titre"], "concerts": found}
        if failure:
            entry["erreur"] = failure
        per_agenda.append(entry)
    if agendas and len(errors) == len(agendas):
        state.pop("openagenda", None)
        codes = sorted({str(e.code or "réseau") for e in errors})
        raise ScanError(f"OpenAgenda a refusé la lecture de tous les agendas ({', '.join(codes)}) : "
                        "vérifiez les droits de la clé.", errors[0].code)
    message = f"{len(concerts)} concert(s) dans {len(agendas) - len(errors)} agenda(s) lisible(s)"
    if errors:
        message += f", {len(errors)} agenda(s) inaccessible(s)"
    if refused:
        message += f", {refused} écarté(s) car leur licence n'est pas ouverte"
    if truncated:
        message += f", lecture limitée pour {truncated} agenda(s)"
    report = {"nom": "OpenAgenda", "type": "openagenda", "ok": True, "message": message, "agendas": per_agenda}
    return list(concerts.values()), report


def flatten(value, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}_{key}".lower().strip("_"))
    elif isinstance(value, list):
        for item in value:
            yield from flatten(item, prefix)
    elif value not in (None, ""):
        yield prefix, value


def find_coordinates(value) -> tuple[float | None, float | None]:
    if isinstance(value, dict):
        if isinstance(value.get("lat"), (int, float)) and isinstance(value.get("lon"), (int, float)):
            return float(value["lat"]), float(value["lon"])
        coordinates = value.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) == 2 and all(isinstance(c, (int, float)) for c in coordinates):
            return float(coordinates[1]), float(coordinates[0])
        for item in value.values():
            found = find_coordinates(item)
            if found[0] is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_coordinates(item)
            if found[0] is not None:
                return found
    return None, None


def clermont_records(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("features", "results", "records"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def parse_clermont_record(record: dict, today: str) -> dict | None:
    """Le fichier suit le format Apidae : les champs sont repérés par leur nom."""
    fields = list(flatten(record))
    text = lambda *needles: [str(v) for k, v in fields if all(n in k for n in needles)]
    titles = text("nom", "libellefr") or text("titre") or text("title") or text("nom")
    categories = " ".join(text("categorie") + text("typesmanifestation") + text("theme"))
    dates = sorted({str(v)[:10] for k, v in fields
                    if "date" in k and ("debut" in k or "start" in k) and re.match(r"\d{4}-\d{2}-\d{2}", str(v))})
    upcoming = [date for date in dates if date >= today]
    if not titles or not upcoming or is_excluded_format(titles[0]) or not looks_like_concert(titles[0], categories):
        return None
    lat, lon = find_coordinates(record)
    identifier = next((str(v) for k, v in fields if k in ("id", "identifiant", "properties_id")), f"{titles[0]}-{upcoming[0]}")
    links = [str(v) for k, v in fields if str(v).startswith("https://") and ("url" in k or "site" in k)
             and not re.search(r"image|illustration|photo|\.jpe?g|\.png", f"{k} {v}", re.I)]
    cities = text("commune", "nom") or text("commune")
    places = text("nomdulieu") or text("lieu") or text("adresse")
    return {
        "id": f"cf-{identifier}",
        "ids": [f"cf-{identifier}"],
        "titre": titles[0],
        "artistes": [],
        "date": upcoming[0],
        "heure": "",
        "salle": places[0] if places else "Lieu à confirmer",
        "salle_id": "",
        "ville": cities[0] if cities else "Clermont-Ferrand",
        "lat": lat,
        "lon": lon,
        "url": links[0] if links else "",
        "styles": sorted({word.strip() for word in text("categorie") if word.strip()}),
        "statut": "",
        "ventes": [],
        "_texte": normalize(categories),
        "source": {"type": "clermont", "nom": CLERMONT_CREDIT, "url": CLERMONT_PAGE, "licence": "Licence Ouverte 2.0",
                   "licence_url": LICENCE_OUVERTE_URL},
    }


def fetch_clermont(now: datetime, loader=None) -> tuple[list[dict], dict]:
    data = (loader or (lambda: fetch_json(CLERMONT_URL, label="L'agenda de Clermont-Ferrand")))()
    records = clermont_records(data)
    today = now.astimezone(PARIS).date().isoformat()
    concerts = []
    for record in records:
        concert = parse_clermont_record(record, today)
        if concert:
            concert["source"]["maj"] = today
            concerts.append(concert)
    report = {"nom": "Agenda de Clermont-Ferrand", "type": "clermont", "ok": True,
              "message": f"{len(concerts)} concert(s) sur {len(records)} événement(s)"}
    if records and not concerts:
        # Sert à ajuster le lecteur si le format du fichier n'est pas celui attendu.
        report["champs"] = sorted({key for key, _ in flatten(records[0])})[:40]
    return concerts, report


# --- Filtres ----------------------------------------------------------------

def match_concert(concert: dict, config: dict, styles_by_id: dict) -> list[str]:
    """Renvoie les raisons de garder le concert, ou une liste vide."""
    reasons = []
    names = normalize(" ".join([concert["titre"], *concert["artistes"], concert.get("_texte", "")]))
    for artist in config.get("artistes", []):
        if contains_phrase(names, normalize(artist)):
            reasons.append(artist)
    genres = normalize(" ".join([*concert["styles"], concert.get("_texte", "")]))
    for style_id in config.get("styles", []):
        style = styles_by_id.get(style_id)
        if style is None:
            continue
        if style_id == "tout" or any(contains_phrase(genres, normalize(word)) for word in style["mots"]):
            reasons.append(style["label"])
    for word in config.get("mots_cles", []):
        if contains_phrase(f"{genres} {names}", normalize(word)):
            reasons.append(word)
    has_style_filter = bool(config.get("styles") or config.get("mots_cles"))
    if not reasons and has_style_filter and not concert["styles"] and config.get("inclure_sans_style", True):
        reasons.append("Style non renseigné")
    return reasons


def select_concerts(raw_events: list[dict], config: dict, styles_by_id: dict, now: datetime,
                    extra: list[dict] | None = None) -> tuple[list[dict], dict]:
    zone = config["zone"]
    margin = zone.get("marge_km", 25)
    today = now.astimezone(PARIS).date().isoformat()
    unique = {event["id"]: event for event in raw_events if event.get("id") and not event.get("test")}

    def usable(concert: dict) -> bool:
        if concert["date"] and concert["date"] < today:
            return False
        return concert["lat"] is None or distance_to_triangle(concert["lat"], concert["lon"], zone["coins"]) <= margin

    ticketmaster = [concert for concert in map(parse_event, unique.values()) if usable(concert)]
    others = [concert for concert in (extra or []) if usable(concert)]
    merged = merge_sources(merge_duplicates(ticketmaster) + others)
    kept = []
    for concert in merged:
        reasons = match_concert(concert, config, styles_by_id)
        concert.pop("_texte", None)
        if reasons:
            concert["raisons"] = reasons
            kept.append(concert)
    kept.sort(key=lambda c: (c["date"] or "9999", normalize(c["titre"])))
    genres = Counter(style for concert in merged for style in concert["styles"])
    counts = {
        "recus": len(unique) + len(extra or []),
        "dans_la_zone": len(merged),
        "retenus": len(kept),
        "sans_style": sum(1 for concert in merged if not concert["styles"]),
    }
    return kept, {"compteurs": counts, "styles_vus": genres.most_common(40)}


# --- Pages des salles -------------------------------------------------------

def page_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "TriangleConcerts/1.0 (veille personnelle)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read(3_000_000).decode("utf-8", errors="ignore")
    content = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", content)
    return normalize(html.unescape(re.sub(r"(?s)<[^>]+>", " ", content)))


def watch_pages(config: dict, known: dict, fetch=page_text) -> tuple[list[dict], list[dict], dict]:
    """Cherche les artistes suivis sur les pages des salles (pas les styles)."""
    hits, reports, updated = [], [], {}
    artists = config.get("artistes", [])
    for page in config.get("pages", []):
        url, name = page.get("url", ""), page.get("nom") or page.get("url", "")
        if not url.startswith(("https://", "http://")):
            continue
        previous = set(known.get(url, []))
        try:
            text = fetch(url)
            if len(text) < 200:
                raise ValueError("page presque vide")
        except Exception as exc:  # Une salle en panne ne doit pas bloquer le scan.
            reports.append({"nom": name, "url": url, "ok": False, "message": type(exc).__name__})
            updated[url] = sorted(previous)
            continue
        found = [artist for artist in artists if contains_phrase(text, normalize(artist))]
        updated[url] = sorted(found)
        reports.append({"nom": name, "url": url, "ok": True, "message": f"{len(found)} artiste(s) repéré(s)"})
        hits += [{"artiste": artist, "salle": name, "url": url, "nouveau": artist not in previous} for artist in found]
    return hits, reports, updated


# --- Comparaison avec le scan précédent ---------------------------------------

def sale_key(sale: dict) -> str:
    return f"{sale['type']}:{sale['nom']}"


def reminder_step(hours_left: float, reminder_days: list[int]) -> int | None:
    """Plus petit rappel atteint : à 3 jours avec [7, 1], renvoie 7."""
    reached = [days for days in sorted(reminder_days) if hours_left <= days * 24]
    return reached[0] if reached else None


def compare(concerts: list[dict], state: dict, reminder_days: list[int], now: datetime, first_run: bool,
            silent_sources: frozenset = frozenset()) -> dict:
    """Met à jour state en place et renvoie ce qu'il faut signaler.

    silent_sources : sources qui viennent d'être branchées. Leurs concerts déjà
    annoncés sont enregistrés sans alerte, comme lors d'un premier scan.
    """
    alerts = {"ventes": [], "nouveaux": [], "annonces": [], "changements": []}
    known = state.setdefault("evenements", {})
    aliases = {alias: key for key, entry in known.items() for alias in entry.get("ids", [])}

    for concert in concerts:
        key = next((aliases[i] for i in concert["ids"] if i in aliases), concert["id"])
        entry = known.get(key)
        is_new = entry is None
        if is_new:
            entry = {"premier_scan": api_date(now), "ids": [], "date": concert["date"],
                     "statut": concert["statut"], "ventes": {}, "rappels": []}
            known[key] = entry
            if not first_run and concert.get("source", {}).get("type") not in silent_sources:
                alerts["nouveaux"].append(concert)
        concert["premier_scan"] = entry["premier_scan"]
        entry["ids"] = sorted(set(entry["ids"]) | set(concert["ids"]))

        if not is_new:
            if entry["statut"] != concert["statut"] and concert["statut"] in CANCELLED_STATUSES:
                alerts["changements"].append({"concert": concert, "type": "statut"})
            elif entry.get("date") != concert["date"] and concert["date"]:
                alerts["changements"].append({"concert": concert, "type": "date", "avant": entry.get("date")})
        entry["statut"], entry["date"] = concert["statut"], concert["date"]

        if concert["statut"] in CANCELLED_STATUSES:
            continue

        for sale in concert["ventes"]:
            start = parse_datetime(sale["debut"])
            if start is None or start <= now:
                continue
            name = sale_key(sale)
            hours_left = (start - now).total_seconds() / 3600
            step = reminder_step(hours_left, reminder_days)
            changed = name in entry["ventes"] and entry["ventes"][name] != sale["debut"]
            if name not in entry["ventes"] or changed:
                if changed:
                    entry["rappels"] = [r for r in entry["rappels"] if not r.startswith(f"{name}|")]
                if not is_new and not first_run:
                    alerts["annonces"].append({"concert": concert, "vente": sale})
                    if step is not None:
                        entry["rappels"].append(f"{name}|{step}")
                elif step is not None and is_new and not first_run:
                    entry["rappels"].append(f"{name}|{step}")
            elif step is not None and not any(
                r.startswith(f"{name}|") and int(r.rsplit("|", 1)[1]) <= step for r in entry["rappels"]
            ):
                entry["rappels"].append(f"{name}|{step}")
                alerts["ventes"].append({"concert": concert, "vente": sale, "heures": hours_left})
            entry["ventes"][name] = sale["debut"]

    limit = (now - timedelta(days=2)).astimezone(PARIS).date().isoformat()
    state["evenements"] = {k: v for k, v in known.items() if not v.get("date") or v["date"] >= limit}
    alerts["ventes"].sort(key=lambda item: item["vente"]["debut"])
    return alerts


# --- Telegram ---------------------------------------------------------------

def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def concert_line(concert: dict) -> str:
    title = esc(concert["titre"])
    if concert.get("url"):
        title = f'<a href="{html.escape(concert["url"], quote=True)}">{title}</a>'
    place = ", ".join(part for part in (concert["salle"], concert["ville"]) if part)
    line = f"• <b>{title}</b>\n  {fr_local_date(concert['date'])}, {esc(place)}"
    source = concert.get("source", {})
    if source.get("type") != "ticketmaster" and source.get("nom"):
        line += f"\n  Source : {esc(source['nom'])}"
    return line


def sale_line(sale: dict) -> str:
    return f"  {esc(sale['nom'])} : {fr_datetime(parse_datetime(sale['debut']))}"


def section(title: str, lines: list[str], site: str) -> list[str]:
    if not lines:
        return []
    shown = lines[:MAX_ALERTS_PER_SECTION]
    if len(lines) > len(shown):
        more = len(lines) - len(shown)
        shown.append(f"… et {more} autre(s) sur {site}" if site else f"… et {more} autre(s) sur la page")
    return [f"<b>{title}</b>", *shown, ""]


def next_sale_line(concert: dict, now: datetime) -> list[str]:
    upcoming = [s for s in concert["ventes"] if (parse_datetime(s["debut"]) or now) > now]
    return [sale_line(upcoming[0])] if upcoming else []


def build_message(alerts: dict, hits: list[dict], site: str, now: datetime) -> str:
    parts: list[str] = []
    sales = []
    for item in alerts["ventes"]:
        hours = item["heures"]
        delay = f"dans {round(hours)} h" if hours < 36 else f"dans {math.ceil(hours / 24)} jours"
        sales.append(f"{concert_line(item['concert'])}\n{sale_line(item['vente'])} ({delay})")
    parts += section("🎟 Mises en vente à venir", sales, site)
    parts += section("📅 Dates de vente annoncées",
                     [f"{concert_line(i['concert'])}\n{sale_line(i['vente'])}" for i in alerts["annonces"]], site)
    parts += section("🆕 Nouveaux concerts",
                     ["\n".join([concert_line(c), *next_sale_line(c, now)]) for c in alerts["nouveaux"]], site)
    changes = []
    for item in alerts["changements"]:
        concert = item["concert"]
        if item["type"] == "statut":
            label = {"canceled": "annulé", "postponed": "reporté, nouvelle date à venir", "rescheduled": "reprogrammé"}
            changes.append(f"{concert_line(concert)}\n  Concert {label.get(concert['statut'], concert['statut'])}")
        else:
            changes.append(f"{concert_line(concert)}\n  Date précédente : {fr_local_date(item.get('avant'))}")
    parts += section("⚠️ Changements", changes, site)
    parts += section("🔎 Repérés sur le site d'une salle",
                     [f"• <b>{esc(h['artiste'])}</b> sur {esc(h['salle'])}\n  {esc(h['url'])}" for h in hits if h["nouveau"]],
                     site)
    return "\n".join(parts).strip()


def split_message(text: str) -> list[str]:
    chunks, current = [], ""
    for block in text.split("\n"):
        if current and len(current) + len(block) + 1 > TELEGRAM_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{block}" if current else block
    return [*chunks, current] if current else chunks


def send_telegram(text: str, token: str, chat_id: str) -> None:
    for chunk in split_message(text):
        body = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20).close()
        except (urllib.error.URLError, TimeoutError) as exc:
            code = getattr(exc, "code", "réseau")
            raise ScanError(f"Telegram a refusé le message ({code}) : vérifiez le jeton et l'identifiant.") from None


# --- Programme principal ----------------------------------------------------

def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def site_url() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        return ""
    owner, name = repository.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def gather_other_sources(config: dict, state: dict, now: datetime) -> tuple[list[dict], list[dict], set]:
    """Une source en panne ne bloque jamais le scan : ses concerts précédents sont gardés."""
    concerts: list[dict] = []
    reports: list[dict] = []
    failed: set = set()
    options = config.get("sources") or {}
    key = os.environ.get("OPENAGENDA_API_KEY", "").strip()
    if key and options.get("openagenda", {}).get("actif", True):
        try:
            found, report = fetch_openagenda(config, key, state, now)
            concerts += found
            reports.append(report)
        except Exception as exc:
            failed.add("openagenda")
            reports.append({"nom": "OpenAgenda", "type": "openagenda", "ok": False,
                            "message": str(exc) if isinstance(exc, ScanError) else "lecture impossible"})
    elif not key:
        reports.append({"nom": "OpenAgenda", "type": "openagenda", "ok": False,
                        "message": "clé OPENAGENDA_API_KEY absente des secrets GitHub"})
    if options.get("clermont", {}).get("actif", True):
        try:
            found, report = fetch_clermont(now)
            concerts += found
            reports.append(report)
        except Exception as exc:
            failed.add("clermont")
            reports.append({"nom": "Agenda de Clermont-Ferrand", "type": "clermont", "ok": False,
                            "message": str(exc) if isinstance(exc, ScanError) else "lecture impossible"})
    return concerts, reports, failed


def first_run_message(concerts: list[dict], site: str) -> str:
    upcoming = sum(1 for concert in concerts if concert["ventes"])
    return (f"✅ Scanner connecté : {len(concerts)} concert(s) trouvé(s), dont {upcoming} avec une date de vente. "
            f"Vous recevrez désormais uniquement les nouveautés et les rappels. {site}").strip()


def run(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    config = read_json(CONFIG_PATH, {})
    styles_by_id = {style["id"]: style for style in read_json(STYLES_PATH, [])}
    state = read_json(STATE_PATH, {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    api_key = os.environ.get("TICKETMASTER_API_KEY", "").strip()
    can_notify = bool(token and chat_id)
    site = site_url()
    status = {"dernier_scan": api_date(now), "ok": False, "erreur": "", "avertissement": "",
              "telegram": "actif" if can_notify else "non configuré"}

    try:
        if not api_key:
            raise ScanError("La clé TICKETMASTER_API_KEY manque dans les secrets GitHub.")
        raw_events, probe_total = fetch_events(config, api_key, now)
        working_state = copy.deepcopy(state)
        extra, source_reports, failed_sources = gather_other_sources(config, working_state, now)
        concerts, stats = select_concerts(raw_events, config, styles_by_id, now, extra)
        known_ids = {i for concert in concerts for i in concert["ids"]}
        previous = read_json(CONCERTS_PATH, {}).get("concerts", [])
        today = now.astimezone(PARIS).date().isoformat()
        concerts += [c for c in previous if c.get("source", {}).get("type") in failed_sources
                     and (c.get("date") or "9999") >= today and not known_ids.intersection(c.get("ids", []))]
        concerts.sort(key=lambda c: (c["date"] or "9999", normalize(c["titre"])))
    except Exception as exc:  # status.json est public : seules les ScanError y sont recopiées.
        reason = str(exc) if isinstance(exc, ScanError) else f"Erreur interne du scanner ({type(exc).__name__})."
        state["echecs"] = state.get("echecs", 0) + 1
        status["erreur"] = reason
        write_json(STATUS_PATH, {**read_json(STATUS_PATH, {}), **status})
        write_json(STATE_PATH, state)
        if can_notify and state["echecs"] == FAILURES_BEFORE_ALERT:
            try:
                send_telegram(f"⚠️ Le scanner échoue depuis {FAILURES_BEFORE_ALERT} scans : {reason}", token, chat_id)
            except ScanError:
                pass
        print(f"Échec du scan : {reason}", file=sys.stderr)
        return 1

    hits, page_reports, page_memory = watch_pages(config, state.get("pages", {}))
    has_filters = bool(config.get("artistes") or config.get("styles") or config.get("mots_cles"))
    first_run = not state.get("initialise")
    updated = working_state
    # Une installation déjà en service ne connaissait que Ticketmaster.
    seen_sources = set(updated.get("sources_vues", [])) or ({"ticketmaster"} if not first_run else set())
    working_sources = {r["type"] for r in source_reports if r["ok"]} | {"ticketmaster"}
    silent = frozenset(working_sources - seen_sources)
    alerts = compare(concerts, updated, config.get("rappels_jours", [7, 1]), now, first_run, silent)
    updated["sources_vues"] = sorted(seen_sources | working_sources)
    # Tant que rien n'est choisi, le prochain scan reste un « premier scan » :
    # enregistrer ses goûts ne doit pas déclencher une avalanche d'alertes.
    updated.update(initialise=has_filters, connecte=True, echecs=0, pages=page_memory)

    status.update(stats)
    status.update(ok=True, pages=page_reports, sources=source_reports)
    if not has_filters:
        status["avertissement"] = "Aucun artiste ni style choisi : ajoutez-en dans les réglages."
    elif probe_total == 0:
        status["avertissement"] = "Ticketmaster n'a renvoyé aucun concert dans cette zone."

    if first_run:
        message = first_run_message(concerts, site) if has_filters or not state.get("connecte") else ""
    else:
        message = build_message(alerts, hits, site, now)
    delivered = True
    if message and can_notify:
        try:
            send_telegram(message, token, chat_id)
        except ScanError as exc:
            # L'état n'est pas enregistré : les alertes repartiront au prochain scan.
            delivered = False
            status["telegram"] = "erreur"
            status["erreur"] = str(exc)

    write_json(CONCERTS_PATH, {"concerts": concerts, "reperes": hits})
    write_json(STATUS_PATH, status)
    if delivered:
        write_json(STATE_PATH, updated)
    print(f"{len(concerts)} concert(s) retenu(s) sur {stats['compteurs']['recus']} reçu(s).")
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(run())
