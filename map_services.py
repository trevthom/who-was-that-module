"""Same-origin map services: map tiles and address look-ups.

The old app asked the browser to talk to ``tile.openstreetmap.org`` and
``nominatim.openstreetmap.org`` directly. The hub's Content Security Policy
only allows ``img-src 'self'`` and ``connect-src 'self'``, so both services are
proxied through Flask instead:

* ``fetch_tile`` streams a single OpenStreetMap tile and caches it in memory.
* ``search_address`` / ``reverse_geocode`` talk to Nominatim at the polite rate
  its usage policy asks for (one request per second, identifying user agent).

Nothing here is required for creating, editing or deleting people: when the
network is unavailable both helpers fail quietly and the app keeps working
without map tiles or address search.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict

USER_AGENT = os.getenv(
    "WHO_WAS_THAT_USER_AGENT",
    "ThompsonFamilyHub/1.0 (self-hosted family hub; Who Was That?)",
)
TILE_URL_TEMPLATE = os.getenv(
    "WHO_WAS_THAT_TILE_URL",
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
)
NOMINATIM_SEARCH_URL = os.getenv(
    "WHO_WAS_THAT_GEOCODE_URL",
    "https://nominatim.openstreetmap.org/search",
)
NOMINATIM_REVERSE_URL = os.getenv(
    "WHO_WAS_THAT_REVERSE_URL",
    "https://nominatim.openstreetmap.org/reverse",
)

TILE_TIMEOUT = float(os.getenv("WHO_WAS_THAT_TILE_TIMEOUT", "6"))
GEOCODE_TIMEOUT = float(os.getenv("WHO_WAS_THAT_GEOCODE_TIMEOUT", "8"))
MAX_TILE_BYTES = 512 * 1024
TILE_CACHE_MAX = 512
TILE_CACHE_TTL = 7 * 24 * 3600
GEOCODE_CACHE_MAX = 200
GEOCODE_CACHE_TTL = 24 * 3600
NOMINATIM_MIN_INTERVAL = 1.1


class MapServiceError(RuntimeError):
    """Raised when an upstream map service cannot answer."""


def tiles_enabled() -> bool:
    """Tiles can be switched off for offline installs and tests."""

    value = os.getenv("WHO_WAS_THAT_TILES", "on")
    return value.strip().lower() not in {"0", "false", "no", "off"}


_tile_cache: "OrderedDict[tuple[int, int, int], tuple[float, bytes]]" = OrderedDict()
_geocode_cache: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
_cache_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


def _transparent_tile() -> bytes:
    """A valid 1x1 transparent PNG, used when a tile cannot be fetched."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00" + b"\x00\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


BLANK_TILE = _transparent_tile()


def fetch_tile(z: int, x: int, y: int) -> bytes | None:
    """Return the PNG bytes for a tile, or ``None`` if it cannot be fetched."""

    if not tiles_enabled():
        return None

    key = (z, x, y)
    now = time.time()
    with _cache_lock:
        cached = _tile_cache.get(key)
        if cached and now - cached[0] < TILE_CACHE_TTL:
            _tile_cache.move_to_end(key)
            return cached[1]

    url = TILE_URL_TEMPLATE.format(z=z, x=x, y=y)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TILE_TIMEOUT) as response:
            if getattr(response, "status", 200) != 200:
                return None
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return None
            payload = response.read(MAX_TILE_BYTES + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None

    if not payload or len(payload) > MAX_TILE_BYTES:
        return None

    with _cache_lock:
        _tile_cache[key] = (now, payload)
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)
    return payload


def _cached(key: str):
    now = time.time()
    with _cache_lock:
        hit = _geocode_cache.get(key)
        if not hit:
            return None
        if now - hit[0] > GEOCODE_CACHE_TTL:
            _geocode_cache.pop(key, None)
            return None
        _geocode_cache.move_to_end(key)
        return hit[1]


def _store(key: str, value) -> None:
    with _cache_lock:
        _geocode_cache[key] = (time.time(), value)
        _geocode_cache.move_to_end(key)
        while len(_geocode_cache) > GEOCODE_CACHE_MAX:
            _geocode_cache.popitem(last=False)


def _nominatim_json(url: str):
    """Fetch JSON from Nominatim, respecting its one-request-per-second rule."""

    global _nominatim_last_call

    with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.time() - _nominatim_last_call)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=GEOCODE_TIMEOUT) as response:
                if getattr(response, "status", 200) != 200:
                    raise MapServiceError("Address search is unavailable right now")
                body = response.read(1024 * 512)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
            raise MapServiceError("Address search is unavailable right now") from error
        finally:
            _nominatim_last_call = time.time()

    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as error:
        raise MapServiceError("Address search is unavailable right now") from error


def search_address(query: str, limit: int = 5) -> list[dict]:
    """Look up a typed address; returns ``[{lat, lng, address}]``."""

    cleaned = (query or "").strip()[:240]
    if not cleaned:
        return []

    cache_key = f"search:{cleaned.lower()}:{limit}"
    cached = _cached(cache_key)
    if cached is not None:
        return list(cached)

    url = (
        f"{NOMINATIM_SEARCH_URL}?"
        + urllib.parse.urlencode(
            {"format": "jsonv2", "limit": limit, "q": cleaned, "addressdetails": 0}
        )
    )
    payload = _nominatim_json(url)
    if not isinstance(payload, list):
        return []

    results: list[dict] = []
    for item in payload[:limit]:
        if not isinstance(item, dict):
            continue
        try:
            lat = round(float(item["lat"]), 6)
            lng = round(float(item["lon"]), 6)
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        results.append(
            {"lat": lat, "lng": lng, "address": str(item.get("display_name") or "")[:300]}
        )

    _store(cache_key, results)
    return results


def reverse_geocode(lat: float, lng: float) -> str | None:
    """Turn coordinates into a human-readable address, or ``None``."""

    cache_key = f"reverse:{lat:.5f},{lng:.5f}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached or None

    url = (
        f"{NOMINATIM_REVERSE_URL}?"
        + urllib.parse.urlencode(
            {"format": "jsonv2", "zoom": 18, "lat": f"{lat:.6f}", "lon": f"{lng:.6f}"}
        )
    )
    payload = _nominatim_json(url)
    address = ""
    if isinstance(payload, dict):
        address = str(payload.get("display_name") or "")[:300]
    _store(cache_key, address)
    return address or None
