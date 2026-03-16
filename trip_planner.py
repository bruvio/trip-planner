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
    RESET  = '\033[0m'
    BOLD   = '\033[1m'
    DIM    = '\033[2m'
    AMBER  = '\033[38;5;214m'
    GREEN  = '\033[38;5;35m'
    RED    = '\033[38;5;196m'
    BLUE   = '\033[38;5;39m'
    PURPLE = '\033[38;5;141m'
    GREY   = '\033[38;5;245m'
    CYAN   = '\033[38;5;51m'

def c(text, color): return f'{color}{text}{C.RESET}'
def bold(text): return f'{C.BOLD}{text}{C.RESET}'

# ─── LOGGING ────────────────────────────────────────────────
log = logging.getLogger('trip_planner')

# ─── HTTP helpers ────────────────────────────────────────────
LAST_NOMINATIM = 0.0

def http_get(url, headers=None):
    log.debug('GET %s', url[:120])
    req = urllib.request.Request(url, headers=headers or {
        'User-Agent': 'RoadTripPlanner/1.0 (CLI)',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
        log.debug('GET %s -> %d bytes', url[:80], len(json.dumps(data)))
        return data

def http_post(url, data, headers=None, retries=4, backoff=5.0, timeout=30):
    body = data.encode('utf-8')
    log.debug('POST %s (%d bytes)', url[:80], len(body))
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers={
            'User-Agent': 'RoadTripPlanner/1.0 (CLI)',
            'Content-Type': 'application/x-www-form-urlencoded',
            **(headers or {})
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                log.debug('POST %s -> OK', url[:80])
                return result
        except urllib.error.HTTPError as e:
            if e.code in (429, 504) and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                log.warning('%d from Overpass, retry %d/%d in %.0fs',
                            e.code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            log.warning('POST %s -> HTTP %d', url[:80], e.code)
            raise

# ─── GEOCODING ───────────────────────────────────────────────
def geocode(query: str) -> dict:
    global LAST_NOMINATIM
    elapsed = time.time() - LAST_NOMINATIM
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    LAST_NOMINATIM = time.time()
    encoded = urllib.parse.quote(query)
    url = f'https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1'
    results = http_get(url)
    if not results:
        raise ValueError(f'Cannot find location: "{query}"')
    r = results[0]
    return {
        'lat': float(r['lat']),
        'lon': float(r['lon']),
        'display_name': r['display_name'],
        'short': r['display_name'].split(',')[0].strip(),
    }

# ─── ROUTING ─────────────────────────────────────────────────
def route_osrm(waypoints: list) -> dict:
    coords = ';'.join(f"{p['lon']},{p['lat']}" for p in waypoints)
    url = (f'https://router.project-osrm.org/route/v1/driving/{coords}'
           f'?overview=full&geometries=geojson&steps=false')
    data = http_get(url)
    if data.get('code') != 'Ok':
        raise RuntimeError('Routing failed: ' + data.get('message', 'unknown error'))
    return data['routes'][0]

# ─── DISTANCE ───────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def nearest_on_route(route_coords, lat, lon):
    return min(haversine(lat, lon, c[1], c[0]) for c in route_coords)

# ─── OVERPASS (around filter, segmented for long routes) ─────
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
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
    return ','.join(f'{pt[1]},{pt[0]}' for pt in coords)

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
        parts.append(f'{tag_filter}(around:{radius},{poly_str});')
    return f'[out:json][timeout:{timeout}];(' + ''.join(parts) + ');out center 200;'

def _run_overpass(query, timeout=60):
    """Execute a single Overpass query, return elements list."""
    log.debug('Overpass query: %d chars, timeout=%ds', len(query), timeout)
    data_enc = urllib.parse.urlencode({'data': query})
    result = http_post(OVERPASS_URL, data_enc, timeout=timeout + 15)
    elements = result.get('elements', [])
    log.debug('Overpass returned %d elements', len(elements))
    return elements

def overpass_combined_query(simplified_coords, skip_types=None, fuel_radius=5000,
                            ev_radius=5000, hotel_radius=10000, rest_radius=2000,
                            timeout=60, progress_fn=None):
    """Query POI types along the route one type at a time per segment.

    Each POI type uses its own lightweight query (2-4 specs) per segment,
    allowing larger segments (50+ points) without timeouts. Types are queried
    sequentially with delays to respect Overpass rate limits.
    """
    skip = skip_types or set()

    poi_passes = []
    if 'fuel' not in skip:
        poi_passes.append(('fuel', [
            ('node["amenity"="fuel"]', fuel_radius),
            ('way["amenity"="fuel"]', fuel_radius),
        ]))
    if 'ev' not in skip:
        poi_passes.append(('ev', [
            ('node["amenity"="charging_station"]', ev_radius),
            ('way["amenity"="charging_station"]', ev_radius),
        ]))
    if 'hotels' not in skip:
        poi_passes.append(('hotels', [
            ('node["tourism"="hotel"]', hotel_radius),
            ('way["tourism"="hotel"]', hotel_radius),
            ('node["tourism"="motel"]', hotel_radius),
            ('way["tourism"="motel"]', hotel_radius),
        ]))
    if 'rest' not in skip:
        poi_passes.append(('rest', [
            ('node["highway"="rest_area"]', rest_radius),
            ('node["highway"="services"]', rest_radius),
        ]))

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
        log.info('Querying %s (%d segments, %d specs per segment)',
                 type_name, len(segments), len(specs))
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
                    eid = (el.get('type', ''), el.get('id', 0))
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        all_elements.append(el)
                        seg_new += 1
                        type_found += 1
                log.info('  %s seg %d/%d: %d raw, %d new (total %d)',
                         type_name, seg_idx + 1, len(segments),
                         len(elements), seg_new, type_found)
            except Exception as e:
                request_count += 1
                type_errors += 1
                log.warning('  %s seg %d/%d: FAILED %s',
                            type_name, seg_idx + 1, len(segments), e)
        log.info('%s complete: %d found, %d segment errors',
                 type_name, type_found, type_errors)
        if progress_fn:
            if type_errors > 0 and type_found == 0:
                progress_fn(type_name, 0, 0, 0,
                            error=f'all {type_errors} segment(s) failed')
            else:
                progress_fn(type_name, len(segments), len(segments), type_found)

    return _classify_elements(all_elements)

def _classify_elements(elements):
    """Sort Overpass elements into POI categories by their OSM tags."""
    pois = {'fuel': [], 'ev': [], 'hotels': [], 'rest': []}
    for el in elements:
        tags = el.get('tags', {})
        if tags.get('amenity') == 'fuel':
            pois['fuel'].append(el)
        elif tags.get('amenity') == 'charging_station':
            pois['ev'].append(el)
        elif tags.get('tourism') in ('hotel', 'motel'):
            pois['hotels'].append(el)
        elif tags.get('highway') in ('rest_area', 'services'):
            pois['rest'].append(el)
    return pois

def elem_name(el):
    t = el.get('tags', {})
    return t.get('name') or t.get('operator') or t.get('brand') or 'Unnamed'

def elem_center(el):
    if el.get('type') == 'node':
        return el.get('lat'), el.get('lon')
    ctr = el.get('center', {})
    return ctr.get('lat'), ctr.get('lon')

# ─── POI detail extractors ──────────────────────────────────
def fuel_detail(el):
    t = el.get('tags', {})
    fuels = [f for f, k in [('Diesel', 'fuel:diesel'), ('Petrol', 'fuel:octane_95'), ('LPG', 'fuel:lpg')] if t.get(k)]
    return ', '.join(fuels) if fuels else t.get('brand', '-')

def ev_detail(el):
    t = el.get('tags', {})
    sockets = [s for s, k in [('Type2', 'socket:type2'), ('CHAdeMO', 'socket:chademo'), ('CCS', 'socket:type2_combo')] if t.get(k)]
    return ', '.join(sockets) if sockets else t.get('network', '-')

def hotel_detail(el):
    t = el.get('tags', {})
    stars = '*' * int(t['stars']) if t.get('stars', '').isdigit() else ''
    return stars or t.get('tourism', '-')

def rest_detail(el):
    t = el.get('tags', {})
    return 'Services' if t.get('highway') == 'services' else 'Rest area'

POI_TYPES = {
    'fuel':   {'title': 'Fuel Stations',       'icon': '⛽',  'detail_fn': fuel_detail,  'col2': 'Details'},
    'ev':     {'title': 'EV Charging Points',   'icon': '⚡',  'detail_fn': ev_detail,    'col2': 'Sockets'},
    'hotels': {'title': 'Hotels Along Route',   'icon': '🏨',  'detail_fn': hotel_detail, 'col2': 'Type'},
    'rest':   {'title': 'Rest Areas / Services','icon': '🅿️',  'detail_fn': rest_detail,  'col2': 'Type'},
}

# ─── COST CALCULATIONS ───────────────────────────────────────
def calc_costs(dist_km: float, args) -> dict:
    sym = {'GBP': '\u00a3', 'EUR': '\u20ac', 'USD': '$'}.get(args.currency, '\u00a3')
    fuel_cost = ev_cost = 0
    refills = 0

    if args.fuel_type == 'electric':
        ev_cost = (dist_km / 100) * args.kwh * args.kwh_price
    else:
        total_l = (dist_km / 100) * args.efficiency
        fuel_cost = total_l * args.fuel_price
        tank_range = (args.tank / args.efficiency) * 100
        refills = max(0, math.ceil(dist_km / tank_range) - 1)

    if args.fuel_type == 'hybrid':
        ev_cost = (dist_km / 100) * args.kwh * args.kwh_price * 0.3

    toll = args.tolls
    total = fuel_cost + ev_cost + toll
    return {
        'fuel_cost': fuel_cost, 'ev_cost': ev_cost,
        'toll': toll, 'total': total, 'refills': refills,
        'sym': sym, 'dist_km': dist_km,
    }

# ─── FORMAT HELPERS ──────────────────────────────────────────
def fmt_dist(m):
    return f'{m/1000:.0f} km ({m/1609.34:.0f} mi)'

def fmt_time(s):
    h, m = divmod(int(s), 3600)
    m //= 60
    return f'{h}h {m:02d}m' if h else f'{m}m'

def fmt_cost(sym, val):
    return f'{sym}{val:.2f}'

# ─── PRINT HELPERS ──────────────────────────────────────────
def section(title, icon=''):
    print()
    print(c('\u2500' * 60, C.GREY))
    print(f'  {icon}  {bold(c(title, C.AMBER))}')
    print(c('\u2500' * 60, C.GREY))

def row(label, value, color=C.CYAN):
    print(f'  {c(label.ljust(28), C.GREY)}{c(value, color)}')

def poi_row(name, detail, dist_km):
    dist_str = f'{dist_km:.1f} km' if dist_km is not None else '-'
    name_trunc = name[:30].ljust(30)
    detail_trunc = detail[:22].ljust(22)
    print(f'  {c(name_trunc, C.CYAN)}  {c(detail_trunc, C.GREY)}  {c(dist_str, C.AMBER)}')

def display_poi_section(key, elements, quiet=False):
    """Print a POI section with consistent formatting."""
    info = POI_TYPES[key]
    section(info['title'], info['icon'])
    count = len(elements)
    print(f'  {c("->", C.GREEN)} Found {c(str(count), C.CYAN)} nearby')
    if not quiet and elements:
        print(f'  {"Name".ljust(30)}  {info["col2"].ljust(22)}  {"Route dist"}')
        print(c('  ' + '-' * 58, C.GREY))
        for el in elements[:15]:
            poi_row(elem_name(el), info['detail_fn'](el), el.get('_dist'))

# ─── INTERACTIVE VEHICLE CONFIG ─────────────────────────────
VEHICLE_PRESETS = {
    '1': ('Compact Diesel',      'diesel',   5.5,  55, 1.45, 'GBP'),
    '2': ('Family Diesel SUV',   'diesel',   7.5,  65, 1.45, 'GBP'),
    '3': ('Compact Petrol',      'petrol',   6.0,  50, 1.55, 'GBP'),
    '4': ('Family Petrol SUV',   'petrol',   9.0,  70, 1.55, 'GBP'),
    '5': ('EV (Tesla-like)',     'electric',  None, None, None, 'GBP'),
    '6': ('Plug-in Hybrid',     'hybrid',    5.0,  45, 1.50, 'GBP'),
}

def _input_float(prompt, default):
    val = input(f'  {prompt} [{default}]: ').strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        print(f'  {c("Invalid number, using default", C.RED)}')
        return default

def _input_choice(prompt, choices, default):
    val = input(f'  {prompt} [{default}]: ').strip().upper()
    if not val:
        return default
    if val in choices:
        return val
    print(f'  {c("Invalid choice, using default", C.RED)}')
    return default

def prompt_vehicle_config(args):
    """Interactively prompt for vehicle configuration."""
    section('Vehicle Configuration', '🚗')
    print()
    print('  Select a vehicle preset or enter custom values:')
    print()
    for key, (name, *_) in VEHICLE_PRESETS.items():
        print(f'    [{key}] {name}')
    print('    [0] Custom')
    print()
    choice = input('  Choice [1]: ').strip() or '1'

    if choice in VEHICLE_PRESETS:
        name, fuel_type, eff, tank, price, currency = VEHICLE_PRESETS[choice]
        print(f'  {c("->", C.GREEN)} Selected: {c(name, C.CYAN)}')
        args.fuel_type = fuel_type
        if fuel_type == 'electric':
            args.kwh = _input_float('kWh/100km', 18)
            args.kwh_price = _input_float('Price per kWh', 0.35)
        else:
            args.efficiency = eff
            args.tank = tank
            args.fuel_price = price
            if fuel_type == 'hybrid':
                args.kwh = _input_float('kWh/100km (EV portion)', 18)
                args.kwh_price = _input_float('Price per kWh', 0.35)
    else:
        print(f'  {c("->", C.GREEN)} Custom configuration')
        ft = input('  Fuel type (petrol/diesel/electric/hybrid) [diesel]: ').strip().lower() or 'diesel'
        if ft not in ('petrol', 'diesel', 'electric', 'hybrid'):
            ft = 'diesel'
        args.fuel_type = ft
        if ft == 'electric':
            args.kwh = _input_float('kWh/100km', 18)
            args.kwh_price = _input_float('Price per kWh', 0.35)
        else:
            args.efficiency = _input_float('Fuel consumption (L/100km)', 6.5)
            args.tank = _input_float('Tank size (L)', 60)
            args.fuel_price = _input_float('Fuel price per litre', 1.45)
            if ft == 'hybrid':
                args.kwh = _input_float('kWh/100km (EV portion)', 18)
                args.kwh_price = _input_float('Price per kWh', 0.35)

    args.currency = _input_choice('Currency (GBP/EUR/USD)', ['GBP', 'EUR', 'USD'],
                                  args.currency or 'GBP')
    args.tolls = _input_float('Known toll costs', args.tolls or 0)

def apply_defaults(args):
    """Fill any remaining None values with sensible defaults."""
    if args.fuel_type is None:
        args.fuel_type = 'diesel'
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
        args.currency = 'GBP'

# ─── MARKDOWN GENERATION ─────────────────────────────────────
def generate_markdown(waypoints, route, pois, costs, args) -> str:
    sym = costs['sym']
    dist_m = route['distance']
    dur_s = route['duration']
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    title = f"{waypoints[0]['short']} -> {waypoints[-1]['short']}"

    lines = [
        f'# Road Trip Report: {title}',
        '',
        f'**Generated:** {now}  ',
        '**Tool:** Road Trip Planner CLI (OpenStreetMap / OSRM / Overpass API)  ',
        '',
        '## Route', '',
    ]
    for i, wp in enumerate(waypoints):
        label = 'Start' if i == 0 else ('End' if i == len(waypoints) - 1 else f'Stop {i}')
        name_parts = wp['display_name'].split(',')
        short_name = name_parts[0].strip()
        if len(name_parts) > 1:
            short_name += ', ' + name_parts[1].strip()
        lines.append(f'- **{label}**: {short_name}')
    lines.append('')

    lines += ['## Journey Summary', '', '| Metric | Value |', '|--------|-------|']
    lines.append(f'| Distance | {fmt_dist(dist_m)} |')
    lines.append(f'| Driving Time | {fmt_time(dur_s)} (without stops) |')
    lines.append(f'| Fuel Type | {args.fuel_type.capitalize()} |')
    if args.fuel_type != 'electric':
        lines.append(f'| Fuel Consumption | {args.efficiency} L/100km |')
        lines.append(f'| Tank Size | {args.tank} L |')
        lines.append(f'| Estimated Fuel Stops | {costs["refills"]} |')
    else:
        lines.append(f'| Consumption | {args.kwh} kWh/100km |')
    lines.append('')

    lines += ['## Cost Estimate', '', '| Item | Cost |', '|------|------|']
    if args.fuel_type != 'electric':
        lines.append(f'| Fuel ({args.fuel_type}) | {sym}{costs["fuel_cost"]:.2f} |')
    if args.fuel_type in ('electric', 'hybrid'):
        lines.append(f'| Charging | {sym}{costs["ev_cost"]:.2f} |')
    lines.append(f'| Tolls | {sym}{costs["toll"]:.2f} |')
    lines.append(f'| **Total** | **{sym}{costs["total"]:.2f}** |')
    lines += ['', '> Costs are estimates only. Actual fuel prices and tolls vary by location and date.', '']

    for key in ('fuel', 'ev', 'hotels', 'rest'):
        info = POI_TYPES[key]
        items = pois.get(key, [])
        lines.append(f'## {info["icon"]} {info["title"]}')
        lines.append('')
        if not items:
            lines.append('_None found near route._')
            lines.append('')
            continue
        lines.append('| Name | Details | Distance from Route |')
        lines.append('|------|---------|---------------------|')
        for el in items[:20]:
            nm = elem_name(el)
            detail = info['detail_fn'](el)
            dist = f'{el["_dist"]:.1f} km' if '_dist' in el else '-'
            lines.append(f'| {nm} | {detail} | {dist} |')
        lines.append('')

    lines += [
        '## Data Sources', '',
        '- Routing: [OSRM](https://project-osrm.org/) (OpenStreetMap data)',
        '- Geocoding: [Nominatim](https://nominatim.openstreetmap.org/)',
        '- POIs: [Overpass API](https://overpass-api.de/) (OpenStreetMap)',
        '- (c) OpenStreetMap contributors',
        '', '---', '_Generated by Road Trip Planner CLI_',
    ]
    return '\n'.join(lines)

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
</html>'''

def generate_map_html(waypoints, route_geometry, pois, title):
    """Generate a standalone HTML file with a Leaflet map."""
    wp_json = json.dumps([
        {'lat': wp['lat'], 'lon': wp['lon'], 'name': wp['short']}
        for wp in waypoints
    ])
    poi_list = []
    for key, items in pois.items():
        info = POI_TYPES.get(key)
        if not info:
            continue
        for el in items[:50]:
            lat, lon = elem_center(el)
            if not lat or not lon:
                continue
            poi_list.append({
                'lat': lat, 'lon': lon,
                'name': elem_name(el),
                'detail': info['detail_fn'](el),
                'dist': f'{el.get("_dist", 0):.1f} km',
                'type': key,
            })
    return MAP_TEMPLATE.format(
        title=title,
        route_geojson=json.dumps(route_geometry),
        waypoints_json=wp_json,
        pois_json=json.dumps(poi_list),
    )

# ─── FILENAME HELPERS ────────────────────────────────────────
def auto_filename(waypoints, ext='md'):
    origin = waypoints[0]['short'].replace(' ', '_').replace('/', '_')
    dest = waypoints[-1]['short'].replace(' ', '_').replace('/', '_')
    date = datetime.now().strftime('%Y%m%d')
    return f'trip_{origin}_{dest}_{date}.{ext}'

# ─── MAIN ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Road Trip Planner -- plan a drive with fuel, EV, hotel & cost info',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--from', dest='origin', required=True, help='Start location')
    parser.add_argument('--to', dest='destination', required=True, help='End location')
    parser.add_argument('--via', dest='stops', action='append', default=[], metavar='PLACE',
                        help='Add a stop (can repeat)')

    # Vehicle (defaults are None to detect explicit usage)
    parser.add_argument('--fuel-type', choices=['petrol', 'diesel', 'electric', 'hybrid'],
                        default=None, help='Fuel type (default: diesel)')
    parser.add_argument('--consumption', '--efficiency', dest='efficiency', type=float,
                        default=None,
                        help='Fuel consumption in L/100km (default: 6.5)')
    parser.add_argument('--tank', type=float, default=None,
                        help='Tank size in litres (default: 60)')
    parser.add_argument('--fuel-price', type=float, default=None,
                        help='Fuel price per litre (default: 1.45)')
    parser.add_argument('--kwh', type=float, default=None,
                        help='EV: kWh per 100km (default: 18)')
    parser.add_argument('--kwh-price', type=float, default=None,
                        help='EV: price per kWh (default: 0.35)')
    parser.add_argument('--tolls', type=float, default=None,
                        help='Known toll costs (default: 0)')
    parser.add_argument('--currency', choices=['GBP', 'EUR', 'USD'], default=None,
                        help='Currency (default: GBP)')

    # Interactive
    parser.add_argument('-i', '--interactive', action='store_true',
                        help='Interactively configure vehicle/cost parameters')

    # POI control
    parser.add_argument('--no-fuel', action='store_true', help='Skip fuel station search')
    parser.add_argument('--no-ev', action='store_true', help='Skip EV charger search')
    parser.add_argument('--no-hotels', action='store_true', help='Skip hotel search')
    parser.add_argument('--no-rest', action='store_true', help='Skip rest area search')
    parser.add_argument('--poi-radius', type=float, default=5.0,
                        help='Max km from route for fuel/EV POIs (default: 5)')

    # Output
    parser.add_argument('--export', nargs='?', const='auto', default='auto',
                        metavar='FILE', help='Export markdown report (default: auto)')
    parser.add_argument('--no-export', action='store_true', help='Suppress report export')
    parser.add_argument('--map', nargs='?', const='auto', default='auto',
                        metavar='FILE', help='Generate HTML map (default: auto)')
    parser.add_argument('--no-map', action='store_true', help='Suppress map generation')
    parser.add_argument('--quiet', action='store_true', help='Suppress detailed POI output')
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Increase log verbosity (-v info, -vv debug)')

    args = parser.parse_args()

    # Configure logging
    log_level = logging.WARNING  # default: warnings only
    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose >= 1:
        log_level = logging.INFO
    logging.basicConfig(
        level=log_level,
        format='  %(levelname)-5s %(message)s',
        stream=sys.stderr,
    )

    # Determine if we should prompt interactively
    vehicle_flags = [args.fuel_type, args.efficiency, args.tank, args.fuel_price,
                     args.kwh, args.kwh_price]
    explicit_vehicle = any(v is not None for v in vehicle_flags)
    is_tty = sys.stdin.isatty()

    print()
    print(c('  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510', C.AMBER))
    print(c('  \u2502  ', C.AMBER) + bold(c('R O A D   T R I P   P L A N N E R', C.AMBER)) + c('  \u2502', C.AMBER))
    print(c('  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518', C.AMBER))

    # Interactive vehicle config
    if args.interactive or (is_tty and not explicit_vehicle):
        try:
            prompt_vehicle_config(args)
        except (EOFError, KeyboardInterrupt):
            print(f'\n  {c("Using defaults", C.GREY)}')

    apply_defaults(args)

    # ── Geocode all locations ──
    section('Geocoding Locations', '🌍')
    all_locs = [args.origin] + args.stops + [args.destination]
    waypoints = []
    for loc in all_locs:
        try:
            print(f'  {c("->", C.GREEN)} Searching: {c(loc, C.CYAN)}', end=' ', flush=True)
            wp = geocode(loc)
            waypoints.append(wp)
            print(c(f'OK  ({wp["lat"]:.4f}, {wp["lon"]:.4f})', C.GREEN))
        except Exception as e:
            print(c(f'FAIL {e}', C.RED))
            sys.exit(1)

    # ── Plan route ──
    section('Calculating Route', '🗺️')
    try:
        print(f'  {c("->", C.GREEN)} Requesting route from OSRM...', end=' ', flush=True)
        route = route_osrm(waypoints)
        print(c('OK', C.GREEN))
    except Exception as e:
        print(c(f'FAIL {e}', C.RED))
        sys.exit(1)

    dist_m = route['distance']
    dist_km = dist_m / 1000
    dur_s = route['duration']
    route_coords = route['geometry']['coordinates']

    print()
    row('From', waypoints[0]['display_name'].split(',')[0].strip())
    for i, wp in enumerate(waypoints[1:-1], 1):
        row(f'Via {i}', wp['display_name'].split(',')[0].strip())
    row('To', waypoints[-1]['display_name'].split(',')[0].strip())
    row('Distance', fmt_dist(dist_m))
    row('Driving Time', fmt_time(dur_s) + '  (no stops)')

    # ── Costs ──
    costs = calc_costs(dist_km, args)
    sym = costs['sym']

    section('Cost Estimate', '💰')
    if args.fuel_type != 'electric':
        row('Fuel type', args.fuel_type)
        row('Fuel consumption', f'{args.efficiency} L/100km')
        row('Tank size', f'{args.tank} L')
        row('Fuel price', f'{sym}{args.fuel_price}/L')
        row('Total fuel', fmt_cost(sym, costs['fuel_cost']))
        row('Estimated refill stops', str(costs['refills']))
    if args.fuel_type in ('electric', 'hybrid'):
        row('Consumption', f'{args.kwh} kWh/100km')
        row('kWh price', f'{sym}{args.kwh_price}')
        row('Total charging', fmt_cost(sym, costs['ev_cost']))
    row('Tolls (manual)', fmt_cost(sym, costs['toll']))
    print()
    print(f'  {c("TOTAL ESTIMATE".ljust(28), C.AMBER)}{bold(c(fmt_cost(sym, costs["total"]), C.AMBER))}')
    print(f'  {c("(costs are estimates only)", C.GREY)}')

    # ── POIs (single combined query) ──
    section('Points of Interest', '📍')
    skip_types = set()
    if args.no_fuel:
        skip_types.add('fuel')
    if args.no_ev:
        skip_types.add('ev')
    if args.no_hotels:
        skip_types.add('hotels')
    if args.no_rest:
        skip_types.add('rest')

    pois = {}
    simplified = simplify_polyline(route_coords)
    if len(simplified) <= 20:
        n_segments = 1
    else:
        n_segments = len(_split_segments(simplified, pts_per_seg=15, overlap=2))
    n_types = sum(1 for s in ['fuel', 'ev', 'hotels', 'rest'] if s not in skip_types)
    total_queries = n_segments * n_types
    est_seconds = int(total_queries * 2)
    print(f'  {c("->", C.GREEN)} Route: {len(route_coords)} pts -> {len(simplified)} pts, {n_segments} segment(s)')
    print(f'  {c("->", C.GREEN)} {total_queries} queries ({n_types} types x {n_segments} segs), est. ~{est_seconds}s')

    def _progress(type_name, _seg, _total, found=0, error=None):
        label = POI_TYPES.get(type_name, {}).get('title', type_name)
        if error:
            print(f'  {c("->", C.RED)} {label}: {error}', flush=True)
        elif found > 0:
            print(f'  {c("->", C.GREEN)} {label}: {c(str(found), C.CYAN)} found', flush=True)
        else:
            print(f'  {c("->", C.GREEN)} {label}: 0 found', flush=True)

    print(f'  {c("->", C.GREEN)} Querying Overpass API...', flush=True)

    try:
        fuel_radius = int(args.poi_radius * 1000)
        ev_radius = int(args.poi_radius * 1000)
        hotel_radius = max(int(args.poi_radius * 1000), 10000)
        rest_radius = max(int(args.poi_radius * 1000), 2000)
        pois = overpass_combined_query(
            simplified, skip_types=skip_types,
            fuel_radius=fuel_radius, ev_radius=ev_radius,
            hotel_radius=hotel_radius, rest_radius=rest_radius,
            progress_fn=_progress,
        )
        total_found = sum(len(v) for v in pois.values())
        print(f'  {c("->", C.GREEN)} {c(f"OK  ({total_found} POIs found)", C.GREEN)}')
    except Exception as e:
        print(f'  {c("FAIL", C.RED)} {e}')
        pois = {'fuel': [], 'ev': [], 'hotels': [], 'rest': []}

    # Compute distances and sort
    for key, items in pois.items():
        for el in items:
            lat, lon = elem_center(el)
            if lat and lon:
                el['_dist'] = nearest_on_route(simplified, lat, lon)
        pois[key] = sorted(
            [el for el in items if '_dist' in el],
            key=lambda e: e['_dist']
        )

    # Display each POI type
    for key in ('fuel', 'ev', 'hotels', 'rest'):
        if key not in skip_types:
            display_poi_section(key, pois.get(key, []), quiet=args.quiet)

    # ── Summary ──
    section('Summary', '📋')
    row('Route', f'{waypoints[0]["short"]} -> {waypoints[-1]["short"]}')
    row('Distance', fmt_dist(dist_m))
    row('Drive time', fmt_time(dur_s))
    row('Fuel cost', fmt_cost(sym, costs['fuel_cost']) if args.fuel_type != 'electric' else '-')
    row('Charging cost', fmt_cost(sym, costs['ev_cost']) if args.fuel_type in ('electric', 'hybrid') else '-')
    row('Toll cost', fmt_cost(sym, costs['toll']))
    row('Fuel stations found', str(len(pois.get('fuel', []))))
    row('EV chargers found', str(len(pois.get('ev', []))))
    row('Hotels found', str(len(pois.get('hotels', []))))
    row('Rest areas found', str(len(pois.get('rest', []))))
    print()
    print(f'  {c("TOTAL TRIP COST ESTIMATE".ljust(28), C.AMBER)}{bold(c(fmt_cost(sym, costs["total"]), C.AMBER))}')

    # ── Export markdown ──
    if not args.no_export and args.export:
        export_path = args.export if args.export != 'auto' else auto_filename(waypoints, 'md')
        section('Exporting Report', '📄')
        try:
            md = generate_markdown(waypoints, route, pois, costs, args)
            out_path = Path(export_path)
            out_path.write_text(md, encoding='utf-8')
            print(f'  {c("OK", C.GREEN)} Report saved to: {bold(c(str(out_path.resolve()), C.CYAN))}')
        except Exception as e:
            print(c(f'  FAIL Export failed: {e}', C.RED))

    # ── Generate map ──
    if not args.no_map and args.map:
        map_path = args.map if args.map != 'auto' else auto_filename(waypoints, 'html')
        section('Generating Map', '🗺️')
        try:
            title = f'{waypoints[0]["short"]} -> {waypoints[-1]["short"]}'
            html = generate_map_html(waypoints, route['geometry'], pois, title)
            out_path = Path(map_path)
            out_path.write_text(html, encoding='utf-8')
            print(f'  {c("OK", C.GREEN)} Map saved to: {bold(c(str(out_path.resolve()), C.CYAN))}')
            try:
                webbrowser.open(out_path.resolve().as_uri())
                print(f'  {c("OK", C.GREEN)} Opened in browser')
            except Exception:
                print(f'  {c("->", C.GREY)} Open the HTML file manually to view the map')
        except Exception as e:
            print(c(f'  FAIL Map generation failed: {e}', C.RED))

    print()
    print(c('  Data: (c) OpenStreetMap contributors  |  Routing: OSRM  |  POIs: Overpass API', C.GREY))
    print()


if __name__ == '__main__':
    main()
