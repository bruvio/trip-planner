#!/usr/bin/env python3
"""
Road Trip Planner — CLI
Uses OpenStreetMap (Nominatim + OSRM + Overpass) — no API keys required.

Usage examples:
  python trip_planner.py --from "Oxford, UK" --to "Rome, Italy"
  python trip_planner.py --from "Oxford" --to "Rome" --via "Lyon" --via "Milan"
  python trip_planner.py --from "Oxford" --to "Edinburgh" --fuel-type diesel --consumption 6.5 --tank 60 --fuel-price 1.45
  python trip_planner.py --from "London" --to "Paris" --fuel-type electric --kwh 18 --kwh-price 0.35
  python trip_planner.py --from "Oxford" --to "Rome" --tolls 85 --currency EUR --export report.md
  python trip_planner.py --from "Oxford" --to "Rome" -i   # interactive vehicle setup
"""

import argparse
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path


# ─── ANSI colours ───────────────────────────────────────────
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    AMBER = "\033[38;5;214m"
    GREEN = "\033[38;5;35m"
    RED = "\033[38;5;196m"
    BLUE = "\033[38;5;39m"
    PURPLE = "\033[38;5;141m"
    GREY = "\033[38;5;245m"
    CYAN = "\033[38;5;51m"


def c(text, color):
    return f"{color}{text}{C.RESET}"


def bold(text):
    return f"{C.BOLD}{text}{C.RESET}"


# ─── LOGGING ────────────────────────────────────────────────
log = logging.getLogger("trip_planner")

# ─── HTTP helpers ────────────────────────────────────────────
LAST_NOMINATIM = 0.0


def http_get(url, headers=None):
    log.debug("GET %s", url[:120])
    req = urllib.request.Request(
        url,
        headers=headers
        or {
            "User-Agent": "RoadTripPlanner/1.0 (CLI)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
        log.debug("GET %s -> %d bytes", url[:80], len(json.dumps(data)))
        return data


def http_post(url, data, headers=None, retries=4, backoff=5.0, timeout=30):
    body = data.encode("utf-8")
    log.debug("POST %s (%d bytes)", url[:80], len(body))
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": "RoadTripPlanner/1.0 (CLI)",
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                log.debug("POST %s -> OK", url[:80])
                return result
        except urllib.error.HTTPError as e:
            if e.code in (429, 504) and attempt < retries - 1:
                wait = backoff * (2**attempt)
                log.warning(
                    "%d from Overpass, retry %d/%d in %.0fs", e.code, attempt + 1, retries, wait
                )
                time.sleep(wait)
                continue
            log.warning("POST %s -> HTTP %d", url[:80], e.code)
            raise


# ─── GEOCODING ───────────────────────────────────────────────
def geocode(query: str) -> dict:
    global LAST_NOMINATIM
    elapsed = time.time() - LAST_NOMINATIM
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    LAST_NOMINATIM = time.time()
    encoded = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1"
    results = http_get(url)
    if not results:
        raise ValueError(f'Cannot find location: "{query}"')
    r = results[0]
    return {
        "lat": float(r["lat"]),
        "lon": float(r["lon"]),
        "display_name": r["display_name"],
        "short": r["display_name"].split(",")[0].strip(),
    }


# ─── ROUTING ─────────────────────────────────────────────────
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"


def _extract_major_roads(steps, source="osrm"):
    """Extract significant road names from route steps, ordered by appearance.

    Groups consecutive steps on the same road, keeps roads with >5km total.
    """
    road_segments = []  # list of {name, distance_km}
    current_name = None
    current_km = 0

    for step in steps:
        if source == "osrm":
            name = step.get("name", "") or step.get("ref", "")
            dist = step.get("distance", 0) / 1000
        else:  # ors
            name = step.get("name", "")
            dist = step.get("distance", 0) / 1000

        # Clean up name
        name = name.strip()
        if not name or name == "-":
            if current_name:
                current_km += dist
            continue

        if name == current_name:
            current_km += dist
        else:
            if current_name and current_km > 0:
                road_segments.append({"name": current_name, "distance_km": current_km})
            current_name = name
            current_km = dist

    if current_name and current_km > 0:
        road_segments.append({"name": current_name, "distance_km": current_km})

    # Keep only significant roads (>5km) and deduplicate preserving order
    seen = set()
    major = []
    for seg in road_segments:
        if seg["distance_km"] >= 5 and seg["name"] not in seen:
            seen.add(seg["name"])
            major.append(seg)

    return major


def _route_ors(waypoints, api_key, alternatives=False, avoid=None):
    """Route via OpenRouteService. Returns list of normalized route dicts."""
    coords = [[p["lon"], p["lat"]] for p in waypoints]
    body = {
        "coordinates": coords,
        "geometry": True,
        "instructions": True,
        "extra_info": ["tollways"],
    }
    if alternatives and len(coords) == 2:
        # ORS only supports alternatives with exactly 2 waypoints
        body["alternative_routes"] = {
            "target_count": 3,
            "share_factor": 0.6,
            "weight_factor": 1.6,
        }
    if avoid:
        body["options"] = {"avoid_features": avoid}

    req_body = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ORS_URL,
        data=req_body,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, application/geo+json",
        },
    )
    log.debug("ORS request: %d bytes, avoid=%s, alternatives=%s", len(req_body), avoid, alternatives)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        log.error("ORS HTTP %d: %s", e.code, error_body[:500])
        raise RuntimeError(f"ORS HTTP {e.code}: {error_body[:200]}") from e

    features = data.get("features", [])
    if not features:
        raise RuntimeError("ORS returned no routes")

    routes = []
    for feat in features:
        props = feat.get("properties", {})
        summary = props.get("summary", {})
        extras = props.get("extras", {})
        segments = props.get("segments", [])

        # Detect ferry from step types (type 6 = ferry)
        # Filter out false positives: bridges/tunnels show as type 6 with 0 distance
        # Real ferries have >1km distance and >5 min duration
        ferry_segments = []
        for seg in segments:
            for step in seg.get("steps", []):
                if step.get("type") == 6:
                    dist_km = step.get("distance", 0) / 1000
                    dur_min = step.get("duration", 0) / 60
                    if dist_km > 1 and dur_min > 5:
                        ferry_segments.append({
                            "name": step.get("name", "Ferry"),
                            "distance_km": dist_km,
                            "duration_min": dur_min,
                        })

        # Toll info from extras
        tollway_summary = extras.get("tollways", {}).get("summary", [])
        toll_pct = 0
        for ts in tollway_summary:
            if ts.get("value") == 1:  # 1 = is tollway
                toll_pct = ts.get("amount", 0)

        toll_km = (summary.get("distance", 0) / 1000) * (toll_pct / 100)

        # Extract major roads (longest steps by distance)
        all_steps = []
        for seg in segments:
            all_steps.extend(seg.get("steps", []))
        major_roads = _extract_major_roads(all_steps, source="ors")

        routes.append({
            "distance": summary.get("distance", 0),
            "duration": summary.get("duration", 0),
            "geometry": feat.get("geometry", {}),
            "ferry_segments": ferry_segments,
            "has_ferry": len(ferry_segments) > 0,
            "has_toll": toll_pct > 0,
            "toll_km": toll_km,
            "toll_pct": toll_pct,
            "major_roads": major_roads,
            "_source": "ors",
        })

    return routes


OSRM_DEFAULT_URL = "https://router.project-osrm.org"
_osrm_fallback_url = None


def _route_osrm(waypoints, alternatives=False, exclude=None, base_url=None):
    """Route via OSRM. Supports self-hosted with exclude=toll,ferry,motorway.

    If the configured URL fails with connection error, automatically falls back
    to the public OSRM demo (without exclude support).
    """
    global _osrm_fallback_url
    base = (_osrm_fallback_url or base_url or OSRM_DEFAULT_URL).rstrip("/")
    is_self_hosted = base != OSRM_DEFAULT_URL
    coords = ";".join(f"{p['lon']},{p['lat']}" for p in waypoints)
    params = "overview=full&geometries=geojson&steps=true"
    if alternatives:
        params += "&alternatives=true"
    if exclude and is_self_hosted:
        params += f"&exclude={exclude}"
    url = f"{base}/route/v1/driving/{coords}?{params}"
    log.debug("OSRM request: %s", url[:120])
    try:
        data = http_get(url)
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        if base != OSRM_DEFAULT_URL and _osrm_fallback_url is None:
            log.warning("OSRM at %s unreachable (%s), falling back to public demo", base, e)
            _osrm_fallback_url = OSRM_DEFAULT_URL
            # Retry without exclude (public demo doesn't support it)
            params_fallback = "overview=full&geometries=geojson&steps=true"
            if alternatives:
                params_fallback += "&alternatives=true"
            url = f"{OSRM_DEFAULT_URL}/route/v1/driving/{coords}?{params_fallback}"
            data = http_get(url)
        else:
            raise
    if data.get("code") != "Ok":
        raise RuntimeError("Routing failed: " + data.get("message", "unknown error"))

    routes = []
    for r in data["routes"]:
        # Detect ferry from step mode
        ferry_segments = []
        for leg in r.get("legs", []):
            for step in leg.get("steps", []):
                if step.get("mode") == "ferry":
                    ferry_segments.append({
                        "name": step.get("name", "Ferry"),
                        "distance_km": step.get("distance", 0) / 1000,
                        "duration_min": step.get("duration", 0) / 60,
                    })

        # Extract major roads
        all_steps = []
        for leg in r.get("legs", []):
            all_steps.extend(leg.get("steps", []))
        major_roads = _extract_major_roads(all_steps, source="osrm")

        routes.append({
            "distance": r["distance"],
            "duration": r["duration"],
            "geometry": r["geometry"],
            "ferry_segments": ferry_segments,
            "has_ferry": len(ferry_segments) > 0,
            "has_toll": False,  # OSRM demo doesn't provide toll data
            "toll_km": 0,
            "toll_pct": 0,
            "major_roads": major_roads,
            "_source": "osrm",
        })

    return routes


def route_request(waypoints, api_key=None, alternatives=False, avoid=None, osrm_url=None):
    """Request route(s). Uses ORS if api_key provided, else OSRM.

    Self-hosted OSRM supports exclude=toll,ferry,motorway. The public demo does not.
    ORS avoid values (tollways, ferries, highways) are mapped to OSRM exclude values
    when using self-hosted OSRM.
    """
    if api_key:
        return _route_ors(waypoints, api_key, alternatives=alternatives, avoid=avoid)

    # Map ORS avoid features to OSRM exclude values for self-hosted
    osrm_exclude = None
    if avoid:
        ors_to_osrm = {"tollways": "toll", "ferries": "ferry", "highways": "motorway"}
        exclude_parts = [ors_to_osrm.get(a, a) for a in avoid]
        osrm_exclude = ",".join(exclude_parts)

    is_self_hosted = osrm_url and osrm_url.rstrip("/") != OSRM_DEFAULT_URL
    if avoid and not is_self_hosted:
        log.warning("--route-mode requires ORS API key or self-hosted OSRM for avoid features")

    return _route_osrm(waypoints, alternatives=alternatives, exclude=osrm_exclude,
                        base_url=osrm_url)


# ─── ROUTE ANALYSIS ─────────────────────────────────────────
# Country bounding boxes for toll rate estimation (lat_min, lat_max, lon_min, lon_max)
COUNTRY_BOXES = {
    'FR': (42.3, 51.1, -5.1, 8.2),
    'IT': (36.6, 47.1, 6.6, 18.5),
    'ES': (36.0, 43.8, -9.3, 3.3),
    'CH': (45.8, 47.8, 5.9, 10.5),
    'AT': (46.4, 49.0, 9.5, 17.2),
    'DE': (47.3, 55.1, 5.9, 15.0),
    'GB': (49.9, 58.7, -8.2, 1.8),
}

# Approximate toll rate per km on motorways (in EUR)
TOLL_RATES_EUR = {
    'FR': 0.09,
    'IT': 0.07,
    'ES': 0.10,
    'CH': 0.00,  # vignette system, flat fee
    'AT': 0.00,  # vignette system, flat fee
    'DE': 0.00,  # no car tolls
    'GB': 0.00,  # no general motorway tolls
}
TOLL_RATE_DEFAULT = 0.08

# Vignette costs (flat fees for countries that use them)
VIGNETTE_COSTS_EUR = {
    'CH': 40.0,  # 1-year vignette (mandatory)
    'AT': 10.0,  # 10-day vignette
}


def _point_country(lat, lon):
    """Guess country from lat/lon using bounding boxes. Returns country code or None."""
    for code, (lat_min, lat_max, lon_min, lon_max) in COUNTRY_BOXES.items():
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return code
    return None


def analyze_route(route):
    """Analyze a normalized route dict for tolls, ferries, countries.

    The route dict comes from either _route_ors() or _route_osrm() and already
    contains has_toll, toll_km, has_ferry, ferry_segments. This function adds
    country detection and toll cost breakdown.
    """
    toll_km = route.get("toll_km", 0)
    has_toll = route.get("has_toll", False)
    has_ferry = route.get("has_ferry", False)
    ferry_segments = route.get("ferry_segments", [])

    # Detect countries from route geometry
    countries_traversed = set()
    toll_km_by_country = {}
    geom_coords = route.get("geometry", {}).get("coordinates", [])
    if geom_coords:
        # Sample every Nth point to determine countries
        sample_step = max(1, len(geom_coords) // 50)
        for i in range(0, len(geom_coords), sample_step):
            pt = geom_coords[i]
            cc = _point_country(pt[1], pt[0])
            if cc:
                countries_traversed.add(cc)

        # Distribute toll_km across countries proportionally
        if has_toll and countries_traversed:
            # Rough: assign toll km based on which toll-likely countries are traversed
            toll_countries = {cc for cc in countries_traversed if TOLL_RATES_EUR.get(cc, 0.01) > 0}
            if toll_countries:
                per_country = toll_km / len(toll_countries)
                for cc in toll_countries:
                    toll_km_by_country[cc] = per_country
            else:
                toll_km_by_country["XX"] = toll_km

    is_channel_crossing = has_ferry and "GB" in countries_traversed and "FR" in countries_traversed

    return {
        "has_toll": has_toll,
        "toll_km": toll_km,
        "toll_km_by_country": toll_km_by_country,
        "has_ferry": has_ferry,
        "ferry_segments": ferry_segments,
        "countries": countries_traversed,
        "is_channel_crossing": is_channel_crossing,
    }


def estimate_toll_cost(analysis, currency="GBP"):
    """Estimate toll costs from route analysis using per-country heuristics."""
    eur_to = {"GBP": 0.86, "EUR": 1.0, "USD": 1.08}.get(currency, 1.0)
    total_eur = 0

    for cc, km in analysis.get("toll_km_by_country", {}).items():
        rate = TOLL_RATES_EUR.get(cc, TOLL_RATE_DEFAULT)
        total_eur += km * rate

    # Add vignette costs for countries traversed
    for cc in analysis.get("countries", set()):
        if cc in VIGNETTE_COSTS_EUR:
            total_eur += VIGNETTE_COSTS_EUR[cc]

    return total_eur * eur_to


def deduplicate_routes(route_list):
    """Remove routes with nearly identical distance (within 1%)."""
    seen = []
    unique = []
    for label, route, analysis in route_list:
        dist = route["distance"]
        is_dup = False
        for seen_dist in seen:
            if abs(dist - seen_dist) / max(seen_dist, 1) < 0.01:
                is_dup = True
                break
        if not is_dup:
            seen.append(dist)
            unique.append((label, route, analysis))
    return unique


# ─── DISTANCE ───────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_on_route(route_coords, lat, lon):
    return min(haversine(lat, lon, c[1], c[0]) for c in route_coords)


# ─── OVERPASS (around filter, segmented for long routes) ─────
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
POINTS_PER_SEGMENT = 25


def simplify_polyline(route_coords, max_points=150):
    """Subsample GeoJSON [lon,lat] coordinates to at most max_points."""
    n = len(route_coords)
    if n <= max_points:
        return list(route_coords)
    step = (n - 1) / (max_points - 1)
    return [route_coords[round(i * step)] for i in range(max_points)]


def _poly_str(coords):
    """Build Overpass around polyline string: lat1,lon1,lat2,lon2,..."""
    return ",".join(f"{pt[1]},{pt[0]}" for pt in coords)


def _split_segments(simplified_coords, pts_per_seg=POINTS_PER_SEGMENT, overlap=3):
    """Split a polyline into overlapping segments."""
    segments = []
    i = 0
    n = len(simplified_coords)
    while i < n:
        end = min(i + pts_per_seg, n)
        segments.append(simplified_coords[i:end])
        i = end - overlap
        if i >= n - overlap:
            break
    return segments


def _build_query(poly_str, poi_specs, timeout):
    """Build an Overpass around query from a list of (tag_filter, radius) specs."""
    parts = []
    for tag_filter, radius in poi_specs:
        parts.append(f"{tag_filter}(around:{radius},{poly_str});")
    return f"[out:json][timeout:{timeout}];(" + "".join(parts) + ");out center 200;"


OVERPASS_PUBLIC = "https://overpass-api.de/api/interpreter"
_overpass_fallback_active = False


def _run_overpass(query, timeout=60, url=None):
    """Execute a single Overpass query, return elements list.

    If the configured URL fails with connection error, automatically falls back
    to the public Overpass API.
    """
    global _overpass_fallback_active
    endpoint = url or OVERPASS_URL
    if _overpass_fallback_active:
        endpoint = OVERPASS_PUBLIC
    log.debug("Overpass query: %d chars, timeout=%ds, url=%s", len(query), timeout, endpoint[:60])
    data_enc = urllib.parse.urlencode({"data": query})
    try:
        result = http_post(endpoint, data_enc, timeout=timeout + 15)
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        if endpoint != OVERPASS_PUBLIC and not _overpass_fallback_active:
            log.warning("Overpass at %s unreachable (%s), falling back to public API", endpoint[:60], e)
            _overpass_fallback_active = True
            endpoint = OVERPASS_PUBLIC
            result = http_post(endpoint, data_enc, timeout=timeout + 15)
        else:
            raise
    elements = result.get("elements", [])
    log.debug("Overpass returned %d elements", len(elements))
    return elements


def overpass_combined_query(
    simplified_coords,
    skip_types=None,
    fuel_radius=5000,
    ev_radius=5000,
    hotel_radius=10000,
    rest_radius=2000,
    timeout=60,
    progress_fn=None,
    overpass_url=None,
):
    """Query POI types along the route one type at a time per segment.

    Each POI type uses its own lightweight query (2-4 specs) per segment,
    allowing larger segments (50+ points) without timeouts. Types are queried
    sequentially with delays to respect public Overpass rate limits.
    Self-hosted Overpass skips delays entirely.
    """
    skip = skip_types or set()

    poi_passes = []
    if "fuel" not in skip:
        poi_passes.append(
            (
                "fuel",
                [
                    ('node["amenity"="fuel"]', fuel_radius),
                    ('way["amenity"="fuel"]', fuel_radius),
                ],
            )
        )
    if "ev" not in skip:
        poi_passes.append(
            (
                "ev",
                [
                    ('node["amenity"="charging_station"]', ev_radius),
                    ('way["amenity"="charging_station"]', ev_radius),
                ],
            )
        )
    if "hotels" not in skip:
        poi_passes.append(
            (
                "hotels",
                [
                    ('node["tourism"="hotel"]', hotel_radius),
                    ('way["tourism"="hotel"]', hotel_radius),
                    ('node["tourism"="motel"]', hotel_radius),
                    ('way["tourism"="motel"]', hotel_radius),
                ],
            )
        )
    if "rest" not in skip:
        poi_passes.append(
            (
                "rest",
                [
                    ('node["highway"="rest_area"]', rest_radius),
                    ('node["highway"="services"]', rest_radius),
                ],
            )
        )

    # Smaller segments (15 pts, ~120km each) keep Overpass processing fast.
    # Short routes (<=20 pts) use a single segment to minimize requests.
    n_pts = len(simplified_coords)
    if n_pts <= 20:
        segments = [simplified_coords]
    else:
        segments = _split_segments(simplified_coords, pts_per_seg=15, overlap=2)
    all_elements = []
    seen_ids = set()
    request_count = 0
    is_local = overpass_url and "localhost" in overpass_url
    delay = 0.0 if is_local else 2.0  # no delay for self-hosted

    for type_name, specs in poi_passes:
        type_found = 0
        type_errors = 0
        log.info(
            "Querying %s (%d segments, %d specs per segment)", type_name, len(segments), len(specs)
        )
        for seg_idx, seg in enumerate(segments):
            if request_count > 0 and delay > 0:
                time.sleep(delay)
            poly = _poly_str(seg)
            query = _build_query(poly, specs, timeout)
            try:
                elements = _run_overpass(query, timeout, url=overpass_url)
                request_count += 1
                seg_new = 0
                for el in elements:
                    eid = (el.get("type", ""), el.get("id", 0))
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        all_elements.append(el)
                        seg_new += 1
                        type_found += 1
                log.info(
                    "  %s seg %d/%d: %d raw, %d new (total %d)",
                    type_name,
                    seg_idx + 1,
                    len(segments),
                    len(elements),
                    seg_new,
                    type_found,
                )
            except Exception as e:
                request_count += 1
                type_errors += 1
                log.warning("  %s seg %d/%d: FAILED %s", type_name, seg_idx + 1, len(segments), e)
        log.info("%s complete: %d found, %d segment errors", type_name, type_found, type_errors)
        if progress_fn:
            if type_errors > 0 and type_found == 0:
                progress_fn(type_name, 0, 0, 0, error=f"all {type_errors} segment(s) failed")
            else:
                progress_fn(type_name, len(segments), len(segments), type_found)

    return _classify_elements(all_elements)


def _classify_elements(elements):
    """Sort Overpass elements into POI categories by their OSM tags."""
    pois = {"fuel": [], "ev": [], "hotels": [], "rest": []}
    for el in elements:
        tags = el.get("tags", {})
        if tags.get("amenity") == "fuel":
            pois["fuel"].append(el)
        elif tags.get("amenity") == "charging_station":
            pois["ev"].append(el)
        elif tags.get("tourism") in ("hotel", "motel"):
            pois["hotels"].append(el)
        elif tags.get("highway") in ("rest_area", "services"):
            pois["rest"].append(el)
    return pois


def elem_name(el):
    t = el.get("tags", {})
    return t.get("name") or t.get("operator") or t.get("brand") or "Unnamed"


def elem_center(el):
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    ctr = el.get("center", {})
    return ctr.get("lat"), ctr.get("lon")


# ─── POI detail extractors ──────────────────────────────────
def fuel_detail(el):
    t = el.get("tags", {})
    fuels = [
        f
        for f, k in [("Diesel", "fuel:diesel"), ("Petrol", "fuel:octane_95"), ("LPG", "fuel:lpg")]
        if t.get(k)
    ]
    return ", ".join(fuels) if fuels else t.get("brand", "-")


def ev_detail(el):
    t = el.get("tags", {})
    sockets = [
        s
        for s, k in [
            ("Type2", "socket:type2"),
            ("CHAdeMO", "socket:chademo"),
            ("CCS", "socket:type2_combo"),
        ]
        if t.get(k)
    ]
    return ", ".join(sockets) if sockets else t.get("network", "-")


def hotel_detail(el):
    t = el.get("tags", {})
    stars = "*" * int(t["stars"]) if t.get("stars", "").isdigit() else ""
    return stars or t.get("tourism", "-")


def rest_detail(el):
    t = el.get("tags", {})
    return "Services" if t.get("highway") == "services" else "Rest area"


POI_TYPES = {
    "fuel": {"title": "Fuel Stations", "icon": "⛽", "detail_fn": fuel_detail, "col2": "Details"},
    "ev": {"title": "EV Charging Points", "icon": "⚡", "detail_fn": ev_detail, "col2": "Sockets"},
    "hotels": {
        "title": "Hotels Along Route",
        "icon": "🏨",
        "detail_fn": hotel_detail,
        "col2": "Type",
    },
    "rest": {
        "title": "Rest Areas / Services",
        "icon": "🅿️",
        "detail_fn": rest_detail,
        "col2": "Type",
    },
}


# ─── COST CALCULATIONS ───────────────────────────────────────
def calc_costs(dist_km: float, args, toll_estimate=0) -> dict:
    sym = {"GBP": "\u00a3", "EUR": "\u20ac", "USD": "$"}.get(args.currency, "\u00a3")
    fuel_cost = ev_cost = 0
    refills = 0

    if args.fuel_type == "electric":
        ev_cost = (dist_km / 100) * args.kwh * args.kwh_price
    else:
        total_l = (dist_km / 100) * args.efficiency
        fuel_cost = total_l * args.fuel_price
        tank_range = (args.tank / args.efficiency) * 100
        refills = max(0, math.ceil(dist_km / tank_range) - 1)

    if args.fuel_type == "hybrid":
        ev_cost = (dist_km / 100) * args.kwh * args.kwh_price * 0.3

    # Use manual --tolls if provided, otherwise use auto-estimate
    toll = args.tolls if args.tolls else toll_estimate
    total = fuel_cost + ev_cost + toll
    return {
        "fuel_cost": fuel_cost,
        "ev_cost": ev_cost,
        "toll": toll,
        "total": total,
        "refills": refills,
        "sym": sym,
        "dist_km": dist_km,
    }


# ─── FORMAT HELPERS ──────────────────────────────────────────
def fmt_dist(m):
    return f"{m / 1000:.0f} km ({m / 1609.34:.0f} mi)"


def fmt_time(s):
    h, m = divmod(int(s), 3600)
    m //= 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_cost(sym, val):
    return f"{sym}{val:.2f}"


# ─── PRINT HELPERS ──────────────────────────────────────────
def section(title, icon=""):
    print()
    print(c("\u2500" * 60, C.GREY))
    print(f"  {icon}  {bold(c(title, C.AMBER))}")
    print(c("\u2500" * 60, C.GREY))


def row(label, value, color=C.CYAN):
    print(f"  {c(label.ljust(28), C.GREY)}{c(value, color)}")


def poi_row(name, detail, dist_km):
    dist_str = f"{dist_km:.1f} km" if dist_km is not None else "-"
    name_trunc = name[:30].ljust(30)
    detail_trunc = detail[:22].ljust(22)
    print(f"  {c(name_trunc, C.CYAN)}  {c(detail_trunc, C.GREY)}  {c(dist_str, C.AMBER)}")


def display_poi_section(key, elements, quiet=False):
    """Print a POI section with consistent formatting."""
    info = POI_TYPES[key]
    section(info["title"], info["icon"])
    count = len(elements)
    print(f"  {c('->', C.GREEN)} Found {c(str(count), C.CYAN)} nearby")
    if not quiet and elements:
        print(f"  {'Name'.ljust(30)}  {info['col2'].ljust(22)}  {'Route dist'}")
        print(c("  " + "-" * 58, C.GREY))
        for el in elements[:15]:
            poi_row(elem_name(el), info["detail_fn"](el), el.get("_dist"))


# ─── INTERACTIVE VEHICLE CONFIG ─────────────────────────────
VEHICLE_PRESETS = {
    "1": ("Compact Diesel", "diesel", 5.5, 55, 1.45, "GBP"),
    "2": ("Family Diesel SUV", "diesel", 7.5, 65, 1.45, "GBP"),
    "3": ("Compact Petrol", "petrol", 6.0, 50, 1.55, "GBP"),
    "4": ("Family Petrol SUV", "petrol", 9.0, 70, 1.55, "GBP"),
    "5": ("EV (Tesla-like)", "electric", None, None, None, "GBP"),
    "6": ("Plug-in Hybrid", "hybrid", 5.0, 45, 1.50, "GBP"),
}


def _input_float(prompt, default):
    val = input(f"  {prompt} [{default}]: ").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        print(f"  {c('Invalid number, using default', C.RED)}")
        return default


def _input_choice(prompt, choices, default):
    val = input(f"  {prompt} [{default}]: ").strip().upper()
    if not val:
        return default
    if val in choices:
        return val
    print(f"  {c('Invalid choice, using default', C.RED)}")
    return default


def prompt_vehicle_config(args):
    """Interactively prompt for vehicle configuration."""
    section("Vehicle Configuration", "🚗")
    print()
    print("  Select a vehicle preset or enter custom values:")
    print()
    for key, (name, *_) in VEHICLE_PRESETS.items():
        print(f"    [{key}] {name}")
    print("    [0] Custom")
    print()
    choice = input("  Choice [3]: ").strip() or "3"

    if choice in VEHICLE_PRESETS:
        name, fuel_type, eff, tank, price, currency = VEHICLE_PRESETS[choice]
        print(f"  {c('->', C.GREEN)} Selected: {c(name, C.CYAN)}")
        args.fuel_type = fuel_type
        if fuel_type == "electric":
            args.kwh = _input_float("kWh/100km", 18)
            args.kwh_price = _input_float("Price per kWh", 0.35)
        else:
            args.efficiency = eff
            args.tank = tank
            args.fuel_price = price
            if fuel_type == "hybrid":
                args.kwh = _input_float("kWh/100km (EV portion)", 18)
                args.kwh_price = _input_float("Price per kWh", 0.35)
    else:
        print(f"  {c('->', C.GREEN)} Custom configuration")
        ft = (
            input("  Fuel type (petrol/diesel/electric/hybrid) [diesel]: ").strip().lower()
            or "diesel"
        )
        if ft not in ("petrol", "diesel", "electric", "hybrid"):
            ft = "diesel"
        args.fuel_type = ft
        if ft == "electric":
            args.kwh = _input_float("kWh/100km", 18)
            args.kwh_price = _input_float("Price per kWh", 0.35)
        else:
            args.efficiency = _input_float("Fuel consumption (L/100km)", 6.5)
            args.tank = _input_float("Tank size (L)", 60)
            args.fuel_price = _input_float("Fuel price per litre", 1.45)
            if ft == "hybrid":
                args.kwh = _input_float("kWh/100km (EV portion)", 18)
                args.kwh_price = _input_float("Price per kWh", 0.35)

    args.currency = _input_choice(
        "Currency (GBP/EUR/USD)", ["GBP", "EUR", "USD"], args.currency or "GBP"
    )
    args.tolls = _input_float("Known toll costs", args.tolls or 0)


def apply_defaults(args):
    """Fill any remaining None values with sensible defaults."""
    if args.fuel_type is None:
        args.fuel_type = "diesel"
    if args.efficiency is None:
        args.efficiency = 6.5
    if args.tank is None:
        args.tank = 60
    if args.fuel_price is None:
        args.fuel_price = 1.45
    if args.kwh is None:
        args.kwh = 18
    if args.kwh_price is None:
        args.kwh_price = 0.35
    if args.tolls is None:
        args.tolls = 0
    if args.currency is None:
        args.currency = "GBP"


# ─── MARKDOWN GENERATION ─────────────────────────────────────
def generate_markdown(waypoints, route, pois, costs, args,
                      route_options=None, selected_label=None, route_analysis=None) -> str:
    sym = costs["sym"]
    dist_m = route["distance"]
    dur_s = route["duration"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{waypoints[0]['short']} -> {waypoints[-1]['short']}"

    lines = [
        f"# Road Trip Report: {title}",
        "",
        f"**Generated:** {now}  ",
        "**Tool:** Road Trip Planner CLI (OpenStreetMap / ORS / Overpass API)  ",
        "",
        "## Route",
        "",
    ]
    for i, wp in enumerate(waypoints):
        label = "Start" if i == 0 else ("End" if i == len(waypoints) - 1 else f"Stop {i}")
        name_parts = wp["display_name"].split(",")
        short_name = name_parts[0].strip()
        if len(name_parts) > 1:
            short_name += ", " + name_parts[1].strip()
        lines.append(f"- **{label}**: {short_name}")
    lines.append("")

    # Route comparison table
    if route_options and len(route_options) > 1:
        lines += ["## Route Options", ""]
        lines.append("| # | Type | Distance | Time | Tolls | Ferry | Est. Total |")
        lines.append("|---|------|----------|------|-------|-------|------------|")
        for i, (lbl, r, analysis) in enumerate(route_options, 1):
            toll_est = estimate_toll_cost(analysis, args.currency)
            route_costs = calc_costs(r["distance"] / 1000, args, toll_estimate=toll_est)
            toll_str = f"{analysis['toll_km']:.0f} km" if analysis["has_toll"] else "No"
            ferry_str = f"{len(analysis['ferry_segments'])}" if analysis["has_ferry"] else "No"
            marker = " *" if lbl == selected_label else ""
            lines.append(
                f"| {i} | **{lbl}**{marker} | {fmt_dist(r['distance'])} | "
                f"{fmt_time(r['duration'])} | {toll_str} | {ferry_str} | "
                f"{sym}{route_costs['total']:.0f} |"
            )
        lines += ["", f"*Selected route: **{selected_label}***", ""]

    # Toll and ferry info
    if route_analysis:
        if route_analysis["has_toll"]:
            lines.append(f"**Toll roads:** {route_analysis['toll_km']:.0f} km on toll roads")
        if route_analysis["has_ferry"]:
            for seg in route_analysis["ferry_segments"]:
                lines.append(
                    f"**Ferry:** {seg['name']} ({seg['distance_km']:.0f} km, ~{seg['duration_min']:.0f} min) "
                    "-- booking required, cost not included"
                )
            if route_analysis.get("is_channel_crossing"):
                lines.append(
                    "**Alternative:** Eurotunnel (Folkestone-Calais): ~35 min, ~GBP 150-200/car"
                )
        if route_analysis["has_toll"] or route_analysis["has_ferry"]:
            lines.append("")

    # Route description (major roads)
    major_roads = route.get("major_roads", [])
    if major_roads:
        lines += ["## Route Description", ""]
        road_names = [f"**{r['name']}** ({r['distance_km']:.0f} km)" for r in major_roads[:15]]
        lines.append("Via: " + " -> ".join(road_names))
        lines.append("")

    lines += ["## Journey Summary", "", "| Metric | Value |", "|--------|-------|"]
    if selected_label:
        lines.append(f"| Route Type | {selected_label} |")
    lines.append(f"| Distance | {fmt_dist(dist_m)} |")
    lines.append(f"| Driving Time | {fmt_time(dur_s)} (without stops) |")
    lines.append(f"| Fuel Type | {args.fuel_type.capitalize()} |")
    if args.fuel_type != "electric":
        lines.append(f"| Fuel Consumption | {args.efficiency} L/100km |")
        lines.append(f"| Tank Size | {args.tank} L |")
        lines.append(f"| Estimated Fuel Stops | {costs['refills']} |")
    else:
        lines.append(f"| Consumption | {args.kwh} kWh/100km |")
    lines.append("")

    lines += ["## Cost Estimate", "", "| Item | Cost |", "|------|------|"]
    if args.fuel_type != "electric":
        lines.append(f"| Fuel ({args.fuel_type}) | {sym}{costs['fuel_cost']:.2f} |")
    if args.fuel_type in ("electric", "hybrid"):
        lines.append(f"| Charging | {sym}{costs['ev_cost']:.2f} |")
    toll_label = "Tolls (estimated)" if not args.tolls and route_analysis and route_analysis["has_toll"] else "Tolls"
    lines.append(f"| {toll_label} | {sym}{costs['toll']:.2f} |")
    lines.append(f"| **Total** | **{sym}{costs['total']:.2f}** |")
    lines += [
        "",
        "> Costs are estimates only. Actual fuel prices and tolls vary by location and date.",
        "",
    ]

    for key in ("fuel", "ev", "hotels", "rest"):
        info = POI_TYPES[key]
        items = pois.get(key, [])
        lines.append(f"## {info['icon']} {info['title']}")
        lines.append("")
        if not items:
            lines.append("_None found near route._")
            lines.append("")
            continue
        lines.append("| Name | Details | Distance from Route |")
        lines.append("|------|---------|---------------------|")
        for el in items[:20]:
            nm = elem_name(el)
            detail = info["detail_fn"](el)
            dist = f"{el['_dist']:.1f} km" if "_dist" in el else "-"
            lines.append(f"| {nm} | {detail} | {dist} |")
        lines.append("")

    lines += [
        "## Data Sources",
        "",
        "- Routing: [OSRM](https://project-osrm.org/) (OpenStreetMap data)",
        "- Geocoding: [Nominatim](https://nominatim.openstreetmap.org/)",
        "- POIs: [Overpass API](https://overpass-api.de/) (OpenStreetMap)",
        "- (c) OpenStreetMap contributors",
        "",
        "---",
        "_Generated by Road Trip Planner CLI_",
    ]
    return "\n".join(lines)


# ─── MAP GENERATION ──────────────────────────────────────────
MAP_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
  #map {{ width: 100%; height: 100vh; }}
  .legend {{
    background: rgba(255,255,255,0.95); padding: 10px 14px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); font-size: 13px; line-height: 1.8;
  }}
  .legend-dot {{
    display: inline-block; width: 12px; height: 12px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle;
  }}
  .route-panel {{
    background: rgba(255,255,255,0.95); padding: 12px 16px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2); font-size: 13px; max-width: 280px;
  }}
  .route-panel h3 {{ margin: 0 0 8px 0; font-size: 14px; }}
  .route-btn {{
    display: block; width: 100%; text-align: left; padding: 8px 10px;
    margin: 4px 0; border: 2px solid #ddd; border-radius: 6px;
    background: white; cursor: pointer; font-size: 12px; font-family: inherit;
  }}
  .route-btn:hover {{ border-color: #f59e0b; background: #fffbeb; }}
  .route-btn.active {{ border-color: #f59e0b; background: #fef3c7; font-weight: bold; }}
  .route-btn .label {{ font-weight: bold; }}
  .route-btn .detail {{ color: #666; font-size: 11px; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
const allRoutes = {all_routes_json};
const waypoints = {waypoints_json};
const pois = {pois_json};

const routeColors = ['#f59e0b', '#3b82f6', '#10b981', '#a855f7', '#ef4444'];
const map = L.map('map');
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}@2x.png', {{
  attribution: '(c) <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, (c) <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 20
}}).addTo(map);

// Draw all routes
const routeLayers = [];
allRoutes.forEach(function(r, i) {{
  const isSelected = r.selected;
  const layer = L.geoJSON(r.geometry, {{
    style: {{
      color: routeColors[i % routeColors.length],
      weight: isSelected ? 5 : 3,
      opacity: isSelected ? 0.9 : 0.4,
      dashArray: isSelected ? null : '8 6'
    }}
  }}).addTo(map);
  layer.bindPopup('<b>' + r.label + '</b><br>' + r.distance + ' | ' + r.duration + '<br>' + r.info);
  routeLayers.push(layer);
}});

// Fit to selected route
const selectedIdx = allRoutes.findIndex(function(r) {{ return r.selected; }});
if (selectedIdx >= 0 && routeLayers[selectedIdx]) {{
  map.fitBounds(routeLayers[selectedIdx].getBounds(), {{ padding: [40, 40] }});
}}

function makeIcon(color, size) {{
  return L.divIcon({{
    className: '',
    html: '<div style="background:' + color + ';width:' + size + 'px;height:' + size + 'px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.4);"></div>',
    iconSize: [size, size],
    iconAnchor: [size/2, size/2],
    popupAnchor: [0, -size/2]
  }});
}}

const wpColors = {{ start: '#22c55e', end: '#ef4444', stop: '#f59e0b' }};
waypoints.forEach(function(wp, i) {{
  const type = i === 0 ? 'start' : i === waypoints.length - 1 ? 'end' : 'stop';
  L.marker([wp.lat, wp.lon], {{ icon: makeIcon(wpColors[type], 22) }})
    .bindPopup('<b>' + wp.name + '</b><br>' + type.charAt(0).toUpperCase() + type.slice(1))
    .addTo(map);
}});

const poiColors = {{ fuel: '#6b7280', ev: '#3b82f6', hotels: '#a855f7', rest: '#92400e' }};
const poiIcons = {{ fuel: '⛽', ev: '⚡', hotels: '🏨', rest: '🅿️' }};
pois.forEach(function(p) {{
  L.circleMarker([p.lat, p.lon], {{
    radius: 7, fillColor: poiColors[p.type] || '#888', color: '#fff',
    weight: 2, opacity: 1, fillOpacity: 0.85
  }})
    .bindPopup('<b>' + (poiIcons[p.type] || '') + ' ' + p.name + '</b><br>' + p.detail + '<br><small>' + p.dist + ' from route</small>')
    .addTo(map);
}});

// Route selector panel (only if multiple routes)
if (allRoutes.length > 1) {{
  const panel = L.control({{ position: 'topright' }});
  panel.onAdd = function() {{
    const div = L.DomUtil.create('div', 'route-panel');
    let html = '<h3>Route Options</h3>';
    allRoutes.forEach(function(r, i) {{
      const color = routeColors[i % routeColors.length];
      const cls = r.selected ? 'route-btn active' : 'route-btn';
      html += '<button class="' + cls + '" data-idx="' + i + '">' +
        '<span class="legend-dot" style="background:' + color + '"></span>' +
        '<span class="label">' + r.label + '</span><br>' +
        '<span class="detail">' + r.distance + ' | ' + r.duration + '</span><br>' +
        '<span class="detail">' + r.info + '</span>' +
        '</button>';
    }});
    div.innerHTML = html;
    L.DomEvent.disableClickPropagation(div);
    div.addEventListener('click', function(e) {{
      const btn = e.target.closest('.route-btn');
      if (!btn) return;
      const idx = parseInt(btn.dataset.idx);
      // Highlight selected route
      routeLayers.forEach(function(layer, li) {{
        layer.setStyle({{
          weight: li === idx ? 5 : 3,
          opacity: li === idx ? 0.9 : 0.4,
          dashArray: li === idx ? null : '8 6'
        }});
        if (li === idx) layer.bringToFront();
      }});
      div.querySelectorAll('.route-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      map.fitBounds(routeLayers[idx].getBounds(), {{ padding: [40, 40] }});
    }});
    return div;
  }};
  panel.addTo(map);
}}

// Legend
const legend = L.control({{ position: 'bottomright' }});
legend.onAdd = function() {{
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML =
    '<b>Trip Map</b><br>' +
    '<span class="legend-dot" style="background:#22c55e"></span>Start<br>' +
    '<span class="legend-dot" style="background:#ef4444"></span>End<br>' +
    '<span class="legend-dot" style="background:#f59e0b"></span>Via stop<br>' +
    '<span class="legend-dot" style="background:#6b7280"></span>Fuel<br>' +
    '<span class="legend-dot" style="background:#3b82f6"></span>EV charger<br>' +
    '<span class="legend-dot" style="background:#a855f7"></span>Hotel<br>' +
    '<span class="legend-dot" style="background:#92400e"></span>Rest area';
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>'''


def generate_map_html(waypoints, route_geometry, pois, title,
                      route_options=None, selected_idx=0):
    """Generate a standalone HTML file with a Leaflet map showing all route options."""
    wp_json = json.dumps(
        [{"lat": wp["lat"], "lon": wp["lon"], "name": wp["short"]} for wp in waypoints]
    )
    poi_list = []
    for key, items in pois.items():
        info = POI_TYPES.get(key)
        if not info:
            continue
        for el in items[:50]:
            lat, lon = elem_center(el)
            if not lat or not lon:
                continue
            poi_list.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "name": elem_name(el),
                    "detail": info["detail_fn"](el),
                    "dist": f"{el.get('_dist', 0):.1f} km",
                    "type": key,
                }
            )

    # Build route options JSON for the map
    routes_json_list = []
    if route_options and len(route_options) > 1:
        for i, (label, r, analysis) in enumerate(route_options):
            toll_str = f"{analysis['toll_km']:.0f} km tolls" if analysis["has_toll"] else "no tolls"
            ferry_str = f", {len(analysis['ferry_segments'])} ferry" if analysis["has_ferry"] else ""
            routes_json_list.append({
                "label": label,
                "geometry": r.get("geometry", {}),
                "distance": fmt_dist(r["distance"]),
                "duration": fmt_time(r["duration"]),
                "info": f"{toll_str}{ferry_str}",
                "selected": i == selected_idx,
            })
    else:
        routes_json_list.append({
            "label": "Route",
            "geometry": route_geometry,
            "distance": "",
            "duration": "",
            "info": "",
            "selected": True,
        })

    return MAP_TEMPLATE.format(
        title=title,
        route_geojson=json.dumps(routes_json_list[selected_idx]["geometry"]),
        all_routes_json=json.dumps(routes_json_list),
        waypoints_json=wp_json,
        pois_json=json.dumps(poi_list),
    )


# ─── FILENAME HELPERS ────────────────────────────────────────
def auto_filename(waypoints, ext="md"):
    origin = waypoints[0]["short"].replace(" ", "_").replace("/", "_")
    dest = waypoints[-1]["short"].replace(" ", "_").replace("/", "_")
    date = datetime.now().strftime("%Y%m%d")
    return f"trip_{origin}_{dest}_{date}.{ext}"


# ─── MAIN ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Road Trip Planner -- plan a drive with fuel, EV, hotel & cost info",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--from", dest="origin", required=True, help="Start location")
    parser.add_argument("--to", dest="destination", required=True, help="End location")
    parser.add_argument(
        "--via",
        dest="stops",
        action="append",
        default=[],
        metavar="PLACE",
        help="Add a stop (can repeat)",
    )

    # API key / server config
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ORS_API_KEY"),
        help="OpenRouteService API key (or set ORS_API_KEY env var)",
    )
    parser.add_argument(
        "--osrm-url",
        default=os.environ.get("OSRM_URL", "https://router.project-osrm.org"),
        help="OSRM server URL (default: public demo; set OSRM_URL for self-hosted)",
    )
    parser.add_argument(
        "--overpass-url",
        default=os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
        help="Overpass API URL (set OVERPASS_URL for self-hosted)",
    )

    # Vehicle (defaults are None to detect explicit usage)
    parser.add_argument(
        "--fuel-type",
        choices=["petrol", "diesel", "electric", "hybrid"],
        default=None,
        help="Fuel type (default: diesel)",
    )
    parser.add_argument(
        "--consumption",
        "--efficiency",
        dest="efficiency",
        type=float,
        default=None,
        help="Fuel consumption in L/100km (default: 6.5)",
    )
    parser.add_argument(
        "--tank", type=float, default=None, help="Tank size in litres (default: 60)"
    )
    parser.add_argument(
        "--fuel-price", type=float, default=None, help="Fuel price per litre (default: 1.45)"
    )
    parser.add_argument("--kwh", type=float, default=None, help="EV: kWh per 100km (default: 18)")
    parser.add_argument(
        "--kwh-price", type=float, default=None, help="EV: price per kWh (default: 0.35)"
    )
    parser.add_argument("--tolls", type=float, default=None, help="Known toll costs (default: 0)")
    parser.add_argument(
        "--currency", choices=["GBP", "EUR", "USD"], default=None, help="Currency (default: GBP)"
    )

    # Interactive
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Interactively configure vehicle/cost parameters",
    )

    # Route mode
    parser.add_argument(
        "--route-mode",
        choices=["fastest", "shortest", "no-tolls", "no-ferries", "scenic", "compare"],
        default=None,
        help="Route selection: fastest, shortest, no-tolls, no-ferries, scenic, compare (default: compare if interactive)",
    )

    # POI control
    parser.add_argument("--no-fuel", action="store_true", help="Skip fuel station search")
    parser.add_argument("--no-ev", action="store_true", help="Skip EV charger search")
    parser.add_argument("--no-hotels", action="store_true", help="Skip hotel search")
    parser.add_argument("--no-rest", action="store_true", help="Skip rest area search")
    parser.add_argument(
        "--poi-radius",
        type=float,
        default=5.0,
        help="Max km from route for fuel/EV POIs (default: 5)",
    )

    # Output
    parser.add_argument(
        "--export",
        nargs="?",
        const="auto",
        default="auto",
        metavar="FILE",
        help="Export markdown report (default: auto)",
    )
    parser.add_argument("--no-export", action="store_true", help="Suppress report export")
    parser.add_argument(
        "--map",
        nargs="?",
        const="auto",
        default="auto",
        metavar="FILE",
        help="Generate HTML map (default: auto)",
    )
    parser.add_argument("--no-map", action="store_true", help="Suppress map generation")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed POI output")
    parser.add_argument(
        "--save-data",
        metavar="FILE",
        help="Save all fetched data (routes, POIs) to JSON for offline rendering",
    )
    parser.add_argument(
        "--load-data",
        metavar="FILE",
        help="Load previously saved data and skip all API calls (render only)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v info, -vv debug)",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.WARNING  # default: warnings only
    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose >= 1:
        log_level = logging.INFO
    logging.basicConfig(
        level=log_level,
        format="  %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )

    # Determine if we should prompt interactively
    vehicle_flags = [
        args.fuel_type,
        args.efficiency,
        args.tank,
        args.fuel_price,
        args.kwh,
        args.kwh_price,
    ]
    explicit_vehicle = any(v is not None for v in vehicle_flags)
    is_tty = sys.stdin.isatty()

    print()
    print(
        c(
            "  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510",
            C.AMBER,
        )
    )
    print(
        c("  \u2502  ", C.AMBER)
        + bold(c("R O A D   T R I P   P L A N N E R", C.AMBER))
        + c("  \u2502", C.AMBER)
    )
    print(
        c(
            "  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518",
            C.AMBER,
        )
    )

    # Interactive vehicle config
    if args.interactive or (is_tty and not explicit_vehicle):
        try:
            prompt_vehicle_config(args)
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {c('Using defaults', C.GREY)}")

    apply_defaults(args)

    # ══════════════════════════════════════════════════════════
    #  PHASE 1: DATA FETCHING (or load from saved file)
    # ══════════════════════════════════════════════════════════
    data_loaded = False
    if args.load_data:
        section("Loading Saved Data", "📂")
        try:
            with open(args.load_data, encoding="utf-8") as f:
                saved = json.load(f)
            waypoints = saved["waypoints"]
            all_route_options = [tuple(x) for x in saved["route_options"]]
            selected_idx = saved.get("selected_idx", 0)
            pois = saved.get("pois", {})
            data_loaded = True
            print(f"  {c('OK', C.GREEN)} Loaded from {c(args.load_data, C.CYAN)}")
            print(f"  {c('->', C.GREEN)} {len(all_route_options)} route(s), {sum(len(v) for v in pois.values())} POIs")
        except Exception as e:
            print(c(f"  FAIL {e}", C.RED))
            sys.exit(1)

    if not data_loaded:
        section("Geocoding Locations", "🌍")
        all_locs = [args.origin] + args.stops + [args.destination]
        waypoints = []
        for loc in all_locs:
            try:
                print(f"  {c('->', C.GREEN)} Searching: {c(loc, C.CYAN)}", end=" ", flush=True)
                wp = geocode(loc)
                waypoints.append(wp)
                print(c(f"OK  ({wp['lat']:.4f}, {wp['lon']:.4f})", C.GREEN))
            except Exception as e:
                print(c(f"FAIL {e}", C.RED))
                sys.exit(1)

        # ── Determine route mode ──
        route_mode = args.route_mode
        if route_mode is None:
            route_mode = "compare" if is_tty else "fastest"

        api_key = args.api_key
        osrm_self_hosted = args.osrm_url.rstrip("/") != OSRM_DEFAULT_URL
        if api_key:
            provider = "OpenRouteService"
        elif osrm_self_hosted:
            provider = f"OSRM ({args.osrm_url})"
        else:
            provider = "OSRM (public demo)"

        # ── Plan route(s) ──
        section("Calculating Routes", "🗺️")
        print(f"  {c('->', C.GREEN)} Provider: {c(provider, C.CYAN)}")
        if not api_key and not osrm_self_hosted and route_mode in ("no-tolls", "no-ferries", "scenic"):
            print(
                f"  {c('!', C.AMBER)} Set ORS_API_KEY or use self-hosted OSRM for toll-free/scenic routing."
            )
            print(f"  {c('!', C.AMBER)} Falling back to fastest.")
            route_mode = "fastest"

        all_route_options = []  # list of (label, route, analysis)

        # Request 1: default route + alternatives
        if route_mode in ("fastest", "shortest", "compare"):
            try:
                print(f"  {c('->', C.GREEN)} Requesting routes...", end=" ", flush=True)
                routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, alternatives=(route_mode == "compare"))
                print(c(f"OK ({len(routes)} option(s))", C.GREEN))
                for i, r in enumerate(routes):
                    label = "Fastest" if i == 0 else f"Alternative {i}"
                    all_route_options.append((label, r, analyze_route(r)))
            except Exception as e:
                print(c(f"FAIL {e}", C.RED))
                sys.exit(1)
        elif route_mode == "no-tolls":
            try:
                print(f"  {c('->', C.GREEN)} Requesting toll-free route...", end=" ", flush=True)
                routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["tollways"])
                print(c("OK", C.GREEN))
                all_route_options.append(("Cheapest", routes[0], analyze_route(routes[0])))
            except Exception as e:
                print(c(f"FAIL {e} (toll-free route may not exist)", C.RED))
                sys.exit(1)
        elif route_mode == "no-ferries":
            try:
                print(f"  {c('->', C.GREEN)} Requesting ferry-free route...", end=" ", flush=True)
                routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["ferries"])
                print(c("OK", C.GREEN))
                all_route_options.append(("No ferry", routes[0], analyze_route(routes[0])))
            except Exception as e:
                print(c(f"FAIL {e}", C.RED))
                sys.exit(1)
        elif route_mode == "scenic":
            try:
                print(f"  {c('->', C.GREEN)} Requesting scenic route (no motorways)...", end=" ", flush=True)
                routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["highways"])
                print(c("OK", C.GREEN))
                all_route_options.append(("Scenic", routes[0], analyze_route(routes[0])))
            except Exception as e:
                print(c(f"FAIL {e}", C.RED))
                sys.exit(1)

        # For compare mode: request toll-free, ferry-free, and scenic variants
        # Works with ORS (api_key) or self-hosted OSRM (osrm_self_hosted)
        can_avoid = api_key or osrm_self_hosted
        if route_mode == "compare" and all_route_options and can_avoid:
            default_analysis = all_route_options[0][2]

            # Toll-free variant (cheapest)
            # With ORS: only if tolls detected. With OSRM: always try (can't detect tolls).
            should_try_toll_free = default_analysis["has_toll"] or osrm_self_hosted
            if should_try_toll_free:
                try:
                    print(f"  {c('->', C.GREEN)} Requesting toll-free route...", end=" ", flush=True)
                    no_toll_routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["tollways"])
                    print(c("OK", C.GREEN))
                    all_route_options.append(
                        ("Cheapest", no_toll_routes[0], analyze_route(no_toll_routes[0]))
                    )
                except Exception:
                    print(c("N/A (no toll-free route exists)", C.GREY))

            # Ferry-free variant
            should_try_ferry_free = default_analysis["has_ferry"] or osrm_self_hosted
            if should_try_ferry_free:
                try:
                    print(f"  {c('->', C.GREEN)} Requesting ferry-free route...", end=" ", flush=True)
                    no_ferry_routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["ferries"])
                    print(c("OK", C.GREEN))
                    all_route_options.append(
                        ("No ferry", no_ferry_routes[0], analyze_route(no_ferry_routes[0]))
                    )
                except Exception:
                    print(c("N/A (no ferry-free route exists)", C.GREY))

            # Scenic variant (avoid motorways)
            try:
                print(f"  {c('->', C.GREEN)} Requesting scenic route...", end=" ", flush=True)
                scenic_routes = route_request(waypoints, api_key, osrm_url=args.osrm_url, avoid=["highways"])
                print(c("OK", C.GREEN))
                all_route_options.append(
                    ("Scenic", scenic_routes[0], analyze_route(scenic_routes[0]))
                )
            except Exception:
                print(c("N/A (scenic route not available)", C.GREY))

        # Deduplicate and apply smart labels
        all_route_options = deduplicate_routes(all_route_options)

        # Label the shortest route if it differs from fastest
        if len(all_route_options) > 1:
            shortest_idx = min(range(len(all_route_options)),
                               key=lambda i: all_route_options[i][1]["distance"])
            fastest_idx = min(range(len(all_route_options)),
                              key=lambda i: all_route_options[i][1]["duration"])
            label, r, a = all_route_options[shortest_idx]
            if shortest_idx != fastest_idx and label.startswith("Alternative"):
                all_route_options[shortest_idx] = ("Shortest", r, a)

        # ── Route comparison table (ViaMichelin style) ──
        if len(all_route_options) > 1 and route_mode == "compare":
            section("Route Options", "🔀")
            sym_preview = {"GBP": "\u00a3", "EUR": "\u20ac", "USD": "$"}.get(args.currency, "\u00a3")
            print()
            for i, (label, r, analysis) in enumerate(all_route_options, 1):
                toll_est = estimate_toll_cost(analysis, args.currency)
                fuel_est = calc_costs(r["distance"] / 1000, args, toll_estimate=toll_est)
                marker = c(f"  [{i}] ", C.CYAN)
                print(f"{marker}{bold(c(label, C.AMBER))}")
                detail_parts = [
                    fmt_dist(r["distance"]),
                    fmt_time(r["duration"]),
                ]
                if analysis["has_toll"]:
                    detail_parts.append(f"tolls: ~{sym_preview}{toll_est:.0f} ({analysis['toll_km']:.0f} km)")
                else:
                    detail_parts.append("no tolls")
                if analysis["has_ferry"]:
                    n_ferries = len(analysis["ferry_segments"])
                    first_name = analysis["ferry_segments"][0]["name"]
                    detail_parts.append(f"ferry: {n_ferries}x ({first_name})")
                detail_parts.append(f"total: ~{sym_preview}{fuel_est['total']:.0f}")
                print(f"      {c(' | ', C.GREY).join(detail_parts)}")
                print()
            print(c("  " + "-" * 60, C.GREY))

            # Prompt for selection if interactive
            if is_tty:
                try:
                    choice = input(f"  Select route [1]: ").strip() or "1"
                    idx = int(choice) - 1
                    if 0 <= idx < len(all_route_options):
                        selected_idx = idx
                    else:
                        selected_idx = 0
                except (ValueError, EOFError, KeyboardInterrupt):
                    selected_idx = 0
            else:
                selected_idx = 0
        elif route_mode == "shortest" and len(all_route_options) > 1:
            # Pick shortest distance
            selected_idx = min(range(len(all_route_options)),
                               key=lambda i: all_route_options[i][1]["distance"])
        else:
            selected_idx = 0

    selected_label, route, route_analysis = all_route_options[selected_idx]
    dist_m = route["distance"]
    dist_km = dist_m / 1000
    dur_s = route["duration"]
    route_coords = route["geometry"]["coordinates"]
    toll_auto_estimate = estimate_toll_cost(route_analysis, args.currency)

    # ══════════════════════════════════════════════════════════
    #  PHASE 2: RENDERING (works with fetched or loaded data)
    # ══════════════════════════════════════════════════════════

    # ── Display selected route ──
    section("Selected Route", "🗺️")
    if len(all_route_options) > 1:
        row("Route type", selected_label)
    row("From", waypoints[0]["display_name"].split(",")[0].strip())
    for i, wp in enumerate(waypoints[1:-1], 1):
        row(f"Via {i}", wp["display_name"].split(",")[0].strip())
    row("To", waypoints[-1]["display_name"].split(",")[0].strip())
    row("Distance", fmt_dist(dist_m))
    row("Driving Time", fmt_time(dur_s) + "  (no stops)")

    # Major roads
    major_roads = route.get("major_roads", [])
    if major_roads:
        road_str = " -> ".join(r["name"] for r in major_roads[:10])
        row("Route via", road_str, C.GREY)

    # Toll/ferry info
    if route_analysis["has_toll"]:
        toll_detail = f"Yes ({route_analysis['toll_km']:.0f} km on toll roads)"
        row("Toll roads", toll_detail, C.AMBER)
    else:
        row("Toll roads", "None detected", C.GREEN)

    if route_analysis["has_ferry"]:
        for seg in route_analysis["ferry_segments"]:
            ferry_detail = f"{seg['name']} ({seg['distance_km']:.0f} km, ~{seg['duration_min']:.0f} min)"
            row("Ferry crossing", ferry_detail, C.BLUE)
        print(f"  {c('  (ferry booking required, cost not included)', C.GREY)}")
        if route_analysis["is_channel_crossing"]:
            print(
                f"  {c('  Eurotunnel (Folkestone-Calais) is an alternative: ~35 min, ~GBP 150-200/car', C.GREY)}"
            )

    # ── Costs ──
    costs = calc_costs(dist_km, args, toll_estimate=toll_auto_estimate)
    sym = costs["sym"]

    section("Cost Estimate", "💰")
    if args.fuel_type != "electric":
        row("Fuel type", args.fuel_type)
        row("Fuel consumption", f"{args.efficiency} L/100km")
        row("Tank size", f"{args.tank} L")
        row("Fuel price", f"{sym}{args.fuel_price}/L")
        row("Total fuel", fmt_cost(sym, costs["fuel_cost"]))
        row("Estimated refill stops", str(costs["refills"]))
    if args.fuel_type in ("electric", "hybrid"):
        row("Consumption", f"{args.kwh} kWh/100km")
        row("kWh price", f"{sym}{args.kwh_price}")
        row("Total charging", fmt_cost(sym, costs["ev_cost"]))
    if args.tolls:
        row("Tolls (manual)", fmt_cost(sym, costs["toll"]))
    elif route_analysis["has_toll"]:
        row("Tolls (estimated)", fmt_cost(sym, costs["toll"]), C.AMBER)
    else:
        row("Tolls", fmt_cost(sym, 0))
    print()
    print(
        f"  {c('TOTAL ESTIMATE'.ljust(28), C.AMBER)}{bold(c(fmt_cost(sym, costs['total']), C.AMBER))}"
    )
    print(f"  {c('(costs are estimates only)', C.GREY)}")

    # ── POIs (single combined query, skipped if data loaded) ──
    if data_loaded:
        # pois already loaded from file; just display
        skip_types = set()
        for key in ("fuel", "ev", "hotels", "rest"):
            items = pois.get(key, [])
            if items:
                display_poi_section(key, items, quiet=args.quiet)
    else:
        section("Points of Interest", "📍")

    if not data_loaded:
        skip_types = set()
        if args.no_fuel:
            skip_types.add("fuel")
        if args.no_ev:
            skip_types.add("ev")
        if args.no_hotels:
            skip_types.add("hotels")
        if args.no_rest:
            skip_types.add("rest")

        pois = {}
        simplified = simplify_polyline(route_coords)
        if len(simplified) <= 20:
            n_segments = 1
        else:
            n_segments = len(_split_segments(simplified, pts_per_seg=15, overlap=2))
        n_types = sum(1 for s in ["fuel", "ev", "hotels", "rest"] if s not in skip_types)
        total_queries = n_segments * n_types
        est_seconds = int(total_queries * 2)
        print(
            f"  {c('->', C.GREEN)} Route: {len(route_coords)} pts -> {len(simplified)} pts, {n_segments} segment(s)"
        )
        print(
            f"  {c('->', C.GREEN)} {total_queries} queries ({n_types} types x {n_segments} segs), est. ~{est_seconds}s"
        )

        def _progress(type_name, _seg, _total, found=0, error=None):
            label = POI_TYPES.get(type_name, {}).get("title", type_name)
            if error:
                print(f"  {c('->', C.RED)} {label}: {error}", flush=True)
            elif found > 0:
                print(f"  {c('->', C.GREEN)} {label}: {c(str(found), C.CYAN)} found", flush=True)
            else:
                print(f"  {c('->', C.GREEN)} {label}: 0 found", flush=True)

        print(f"  {c('->', C.GREEN)} Querying Overpass API...", flush=True)

        try:
            fuel_radius = int(args.poi_radius * 1000)
            ev_radius = int(args.poi_radius * 1000)
            hotel_radius = max(int(args.poi_radius * 1000), 10000)
            rest_radius = max(int(args.poi_radius * 1000), 2000)
            pois = overpass_combined_query(
                simplified,
                skip_types=skip_types,
                fuel_radius=fuel_radius,
                ev_radius=ev_radius,
                hotel_radius=hotel_radius,
                rest_radius=rest_radius,
                progress_fn=_progress,
                overpass_url=args.overpass_url,
            )
            total_found = sum(len(v) for v in pois.values())
            print(f"  {c('->', C.GREEN)} {c(f'OK  ({total_found} POIs found)', C.GREEN)}")
        except Exception as e:
            print(f"  {c('FAIL', C.RED)} {e}")
            pois = {"fuel": [], "ev": [], "hotels": [], "rest": []}

        # Compute distances and sort
        for key, items in pois.items():
            for el in items:
                lat, lon = elem_center(el)
                if lat and lon:
                    el["_dist"] = nearest_on_route(simplified, lat, lon)
            pois[key] = sorted([el for el in items if "_dist" in el], key=lambda e: e["_dist"])

        # Display each POI type
        for key in ("fuel", "ev", "hotels", "rest"):
            if key not in skip_types:
                display_poi_section(key, pois.get(key, []), quiet=args.quiet)

    # ── Save data (after all API calls complete) ──
    if args.save_data and not data_loaded:
        section("Saving Data", "💾")
        try:
            # Convert sets to lists for JSON serialization
            serializable_options = []
            for label, r, analysis in all_route_options:
                a_copy = dict(analysis)
                if "countries" in a_copy and isinstance(a_copy["countries"], set):
                    a_copy["countries"] = list(a_copy["countries"])
                serializable_options.append([label, r, a_copy])
            save_obj = {
                "waypoints": waypoints,
                "route_options": serializable_options,
                "selected_idx": selected_idx,
                "pois": pois,
            }
            Path(args.save_data).write_text(
                json.dumps(save_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"  {c('OK', C.GREEN)} Data saved to {c(args.save_data, C.CYAN)}")
        except Exception as e:
            print(c(f"  FAIL {e}", C.RED))

    # ── Summary ──
    section("Summary", "📋")
    row("Route", f"{waypoints[0]['short']} -> {waypoints[-1]['short']}")
    if len(all_route_options) > 1:
        row("Route type", selected_label)
    row("Distance", fmt_dist(dist_m))
    row("Drive time", fmt_time(dur_s))
    if route_analysis["has_toll"]:
        row("Toll roads", f"{route_analysis['toll_km']:.0f} km")
    if route_analysis["has_ferry"]:
        row("Ferry crossings", str(len(route_analysis["ferry_segments"])))
    row("Fuel cost", fmt_cost(sym, costs["fuel_cost"]) if args.fuel_type != "electric" else "-")
    row(
        "Charging cost",
        fmt_cost(sym, costs["ev_cost"]) if args.fuel_type in ("electric", "hybrid") else "-",
    )
    toll_label = "Toll cost (estimated)" if not args.tolls and route_analysis["has_toll"] else "Toll cost"
    row(toll_label, fmt_cost(sym, costs["toll"]))
    row("Fuel stations found", str(len(pois.get("fuel", []))))
    row("EV chargers found", str(len(pois.get("ev", []))))
    row("Hotels found", str(len(pois.get("hotels", []))))
    row("Rest areas found", str(len(pois.get("rest", []))))
    print()
    print(
        f"  {c('TOTAL TRIP COST ESTIMATE'.ljust(28), C.AMBER)}{bold(c(fmt_cost(sym, costs['total']), C.AMBER))}"
    )

    # ── Export markdown ──
    if not args.no_export and args.export:
        export_path = args.export if args.export != "auto" else auto_filename(waypoints, "md")
        section("Exporting Report", "📄")
        try:
            md = generate_markdown(
                waypoints, route, pois, costs, args,
                route_options=all_route_options,
                selected_label=selected_label,
                route_analysis=route_analysis,
            )
            out_path = Path(export_path)
            out_path.write_text(md, encoding="utf-8")
            print(
                f"  {c('OK', C.GREEN)} Report saved to: {bold(c(str(out_path.resolve()), C.CYAN))}"
            )
        except Exception as e:
            print(c(f"  FAIL Export failed: {e}", C.RED))

    # ── Generate map ──
    if not args.no_map and args.map:
        map_path = args.map if args.map != "auto" else auto_filename(waypoints, "html")
        section("Generating Map", "🗺️")
        try:
            title = f"{waypoints[0]['short']} -> {waypoints[-1]['short']}"
            html = generate_map_html(
                waypoints, route["geometry"], pois, title,
                route_options=all_route_options,
                selected_idx=selected_idx,
            )
            out_path = Path(map_path)
            out_path.write_text(html, encoding="utf-8")
            print(f"  {c('OK', C.GREEN)} Map saved to: {bold(c(str(out_path.resolve()), C.CYAN))}")
            try:
                webbrowser.open(out_path.resolve().as_uri())
                print(f"  {c('OK', C.GREEN)} Opened in browser")
            except Exception:
                print(f"  {c('->', C.GREY)} Open the HTML file manually to view the map")
        except Exception as e:
            print(c(f"  FAIL Map generation failed: {e}", C.RED))

    print()
    print(
        c("  Data: (c) OpenStreetMap contributors  |  Routing: OSRM  |  POIs: Overpass API", C.GREY)
    )
    print()


if __name__ == "__main__":
    main()
