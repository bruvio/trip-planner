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
def route_osrm(waypoints: list, alternatives=False, exclude=None, steps=True) -> list:
    """Request route(s) from OSRM. Returns a list of route dicts."""
    coords = ";".join(f"{p['lon']},{p['lat']}" for p in waypoints)
    params = f"overview=full&geometries=geojson&steps={'true' if steps else 'false'}"
    if alternatives:
        params += "&alternatives=true"
    if exclude:
        params += f"&exclude={exclude}"
    url = f"https://router.project-osrm.org/route/v1/driving/{coords}?{params}"
    log.debug("OSRM request: %s", url[:120])
    data = http_get(url)
    if data.get("code") != "Ok":
        raise RuntimeError("Routing failed: " + data.get("message", "unknown error"))
    return data["routes"]


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
    """Analyze a route's steps for tolls, ferries, tunnels.

    Args:
        route: OSRM route dict with legs/steps.

    Returns:
        Dict with toll/ferry analysis results.
    """
    has_toll = False
    has_ferry = False
    ferry_segments = []
    toll_km = 0
    toll_km_by_country = {}
    countries_traversed = set()

    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            classes = step.get("intersections", [{}])[0].get("classes", []) if step.get("intersections") else []
            # Also check step-level classes (OSRM version dependent)
            step_classes = step.get("classes", [])
            all_classes = set(classes) | set(step_classes)

            # Detect country from step geometry
            geom = step.get("geometry", {}).get("coordinates", [])
            if geom:
                mid = geom[len(geom) // 2]
                country = _point_country(mid[1], mid[0])
                if country:
                    countries_traversed.add(country)

            if "toll" in all_classes:
                has_toll = True
                step_km = step.get("distance", 0) / 1000
                toll_km += step_km
                if geom:
                    mid = geom[len(geom) // 2]
                    cc = _point_country(mid[1], mid[0]) or "XX"
                    toll_km_by_country[cc] = toll_km_by_country.get(cc, 0) + step_km

            if step.get("mode") == "ferry" or "ferry" in all_classes:
                has_ferry = True
                ferry_segments.append(
                    {
                        "name": step.get("name", "Ferry"),
                        "distance_km": step.get("distance", 0) / 1000,
                        "duration_min": step.get("duration", 0) / 60,
                    }
                )

    # Check if this is a UK-France ferry (Eurotunnel suggestion)
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


def _run_overpass(query, timeout=60):
    """Execute a single Overpass query, return elements list."""
    log.debug("Overpass query: %d chars, timeout=%ds", len(query), timeout)
    data_enc = urllib.parse.urlencode({"data": query})
    result = http_post(OVERPASS_URL, data_enc, timeout=timeout + 15)
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
):
    """Query POI types along the route one type at a time per segment.

    Each POI type uses its own lightweight query (2-4 specs) per segment,
    allowing larger segments (50+ points) without timeouts. Types are queried
    sequentially with delays to respect Overpass rate limits.
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

    for type_name, specs in poi_passes:
        type_found = 0
        type_errors = 0
        log.info(
            "Querying %s (%d segments, %d specs per segment)", type_name, len(segments), len(specs)
        )
        for seg_idx, seg in enumerate(segments):
            if request_count > 0:
                time.sleep(2.0)
            poly = _poly_str(seg)
            query = _build_query(poly, specs, timeout)
            try:
                elements = _run_overpass(query, timeout)
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
    choice = input("  Choice [1]: ").strip() or "1"

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
def generate_markdown(waypoints, route, pois, costs, args) -> str:
    sym = costs["sym"]
    dist_m = route["distance"]
    dur_s = route["duration"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{waypoints[0]['short']} -> {waypoints[-1]['short']}"

    lines = [
        f"# Road Trip Report: {title}",
        "",
        f"**Generated:** {now}  ",
        "**Tool:** Road Trip Planner CLI (OpenStreetMap / OSRM / Overpass API)  ",
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

    lines += ["## Journey Summary", "", "| Metric | Value |", "|--------|-------|"]
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
    lines.append(f"| Tolls | {sym}{costs['toll']:.2f} |")
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
MAP_TEMPLATE = """<!DOCTYPE html>
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
</style>
</head>
<body>
<div id="map"></div>
<script>
const routeGeoJSON = {route_geojson};
const waypoints = {waypoints_json};
const pois = {pois_json};

const map = L.map('map');
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}@2x.png', {{
  attribution: '(c) <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, (c) <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 20
}}).addTo(map);

const routeLayer = L.geoJSON(routeGeoJSON, {{
  style: {{ color: '#f59e0b', weight: 5, opacity: 0.85 }}
}}).addTo(map);

map.fitBounds(routeLayer.getBounds(), {{ padding: [40, 40] }});

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
</html>"""


def generate_map_html(waypoints, route_geometry, pois, title):
    """Generate a standalone HTML file with a Leaflet map."""
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
    return MAP_TEMPLATE.format(
        title=title,
        route_geojson=json.dumps(route_geometry),
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

    # ── Geocode all locations ──
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

    # ── Plan route(s) ──
    section("Calculating Routes", "🗺️")

    all_route_options = []  # list of (label, route, analysis)

    # Request 1: default route + alternatives
    if route_mode in ("fastest", "shortest", "compare"):
        try:
            print(f"  {c('->', C.GREEN)} Requesting routes from OSRM...", end=" ", flush=True)
            routes = route_osrm(waypoints, alternatives=(route_mode == "compare"), steps=True)
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
            routes = route_osrm(waypoints, exclude="toll", steps=True)
            print(c("OK", C.GREEN))
            all_route_options.append(("Toll-free", routes[0], analyze_route(routes[0])))
        except Exception as e:
            print(c(f"FAIL {e} (toll-free route may not exist)", C.RED))
            sys.exit(1)
    elif route_mode == "no-ferries":
        try:
            print(f"  {c('->', C.GREEN)} Requesting ferry-free route...", end=" ", flush=True)
            routes = route_osrm(waypoints, exclude="ferry", steps=True)
            print(c("OK", C.GREEN))
            all_route_options.append(("No ferry", routes[0], analyze_route(routes[0])))
        except Exception as e:
            print(c(f"FAIL {e}", C.RED))
            sys.exit(1)
    elif route_mode == "scenic":
        try:
            print(f"  {c('->', C.GREEN)} Requesting scenic route (no motorways)...", end=" ", flush=True)
            routes = route_osrm(waypoints, exclude="motorway", steps=True)
            print(c("OK", C.GREEN))
            all_route_options.append(("Scenic", routes[0], analyze_route(routes[0])))
        except Exception as e:
            print(c(f"FAIL {e}", C.RED))
            sys.exit(1)

    # For compare mode: request toll-free, ferry-free, and scenic variants
    if route_mode == "compare" and all_route_options:
        default_analysis = all_route_options[0][2]

        # Toll-free variant (cheapest)
        if default_analysis["has_toll"]:
            try:
                print(f"  {c('->', C.GREEN)} Requesting toll-free route...", end=" ", flush=True)
                no_toll_routes = route_osrm(waypoints, exclude="toll", steps=True)
                print(c("OK", C.GREEN))
                all_route_options.append(
                    ("Cheapest", no_toll_routes[0], analyze_route(no_toll_routes[0]))
                )
            except Exception:
                print(c("N/A (no toll-free route exists)", C.GREY))

        # Ferry-free variant
        if default_analysis["has_ferry"]:
            try:
                print(f"  {c('->', C.GREEN)} Requesting ferry-free route...", end=" ", flush=True)
                no_ferry_routes = route_osrm(waypoints, exclude="ferry", steps=True)
                print(c("OK", C.GREEN))
                all_route_options.append(
                    ("No ferry", no_ferry_routes[0], analyze_route(no_ferry_routes[0]))
                )
            except Exception:
                print(c("N/A (no ferry-free route exists)", C.GREY))

        # Scenic variant (avoid motorways)
        try:
            print(f"  {c('->', C.GREEN)} Requesting scenic route...", end=" ", flush=True)
            scenic_routes = route_osrm(waypoints, exclude="motorway", steps=True)
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
                names = [s["name"] for s in analysis["ferry_segments"]]
                detail_parts.append(f"ferry: {', '.join(names)}")
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

    # ── POIs (single combined query) ──
    section("Points of Interest", "📍")
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
            md = generate_markdown(waypoints, route, pois, costs, args)
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
            html = generate_map_html(waypoints, route["geometry"], pois, title)
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
