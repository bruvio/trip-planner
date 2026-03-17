#!/bin/bash
# Setup self-hosted services for the trip planner
# Gives you sub-second routing, POI queries, and geocoding -- fully offline
#
# Services:
#   OSRM       - routing (<100ms per request, exclude=toll/ferry/motorway)
#   Overpass   - POI queries (<1s per query, no rate limits)
#   Nominatim  - geocoding (<50ms per query, no rate limits)  [optional, large]
#
# Requirements: docker, docker-compose
# Disk space: OSRM ~30GB, Overpass ~30GB, Nominatim ~50GB
#
# Usage:
#   ./setup-local.sh setup          # Download + process + start all
#   ./setup-local.sh setup osrm     # Setup only OSRM
#   ./setup-local.sh setup overpass # Setup only Overpass
#   ./setup-local.sh start          # Start all services
#   ./setup-local.sh stop           # Stop all services
#   ./setup-local.sh status         # Check what's running
#
# After setup, add to your shell profile:
#   export OSRM_URL=http://localhost:5000
#   export OVERPASS_URL=http://localhost:12346/api/interpreter
#   export NOMINATIM_URL=http://localhost:8088

set -euo pipefail

DATA_DIR="${TRIP_PLANNER_DATA:-$HOME/.trip-planner-data}"
PBF_URL="https://download.geofabrik.de/europe-latest.osm.pbf"
PBF_FILE="$DATA_DIR/europe-latest.osm.pbf"

OSRM_PORT="${OSRM_PORT:-5000}"
OVERPASS_PORT="${OVERPASS_PORT:-12346}"
NOMINATIM_PORT="${NOMINATIM_PORT:-8088}"

# ── Helpers ──────────────────────────────────────────────────
download_pbf() {
    mkdir -p "$DATA_DIR"
    if [ -f "$PBF_FILE" ]; then
        echo "PBF file already exists at $PBF_FILE"
        return
    fi
    echo "Downloading Europe map (~25GB)..."
    wget -c -O "$PBF_FILE" "$PBF_URL"
}

# ── OSRM ─────────────────────────────────────────────────────
setup_osrm() {
    echo "=== Setting up OSRM ==="
    download_pbf
    local osrm_dir="$DATA_DIR/osrm"
    mkdir -p "$osrm_dir"

    # Copy PBF if not already in osrm dir
    if [ ! -f "$osrm_dir/europe-latest.osm.pbf" ]; then
        ln -sf "$PBF_FILE" "$osrm_dir/europe-latest.osm.pbf"
    fi

    if [ -f "$osrm_dir/europe-latest.osrm" ]; then
        echo "OSRM data already processed, skipping."
    else
        echo "Extracting road network (~15 min)..."
        docker run --rm -v "$osrm_dir:/data" osrm/osrm-backend \
            osrm-extract -p /opt/car.lua /data/europe-latest.osm.pbf

        echo "Partitioning (~5 min)..."
        docker run --rm -v "$osrm_dir:/data" osrm/osrm-backend \
            osrm-partition /data/europe-latest.osrm

        echo "Customizing (~5 min)..."
        docker run --rm -v "$osrm_dir:/data" osrm/osrm-backend \
            osrm-customize /data/europe-latest.osrm
    fi
    start_osrm
}

start_osrm() {
    docker stop osrm-europe 2>/dev/null && docker rm osrm-europe 2>/dev/null || true
    local osrm_dir="$DATA_DIR/osrm"
    echo "Starting OSRM on port $OSRM_PORT..."
    docker run -d --name osrm-europe --restart unless-stopped \
        -p "$OSRM_PORT:5000" -v "$osrm_dir:/data" \
        osrm/osrm-backend osrm-routed --algorithm mld /data/europe-latest.osrm
    echo "OSRM running at http://localhost:$OSRM_PORT"
}

# ── Overpass ─────────────────────────────────────────────────
setup_overpass() {
    echo "=== Setting up Overpass API ==="
    download_pbf
    local overpass_dir="$DATA_DIR/overpass"
    mkdir -p "$overpass_dir/db"

    # Copy PBF
    if [ ! -f "$overpass_dir/europe-latest.osm.pbf" ]; then
        ln -sf "$PBF_FILE" "$overpass_dir/europe-latest.osm.pbf"
    fi

    echo "Importing into Overpass (~30 min for Europe)..."
    docker stop overpass-europe 2>/dev/null && docker rm overpass-europe 2>/dev/null || true
    docker run -d --name overpass-europe --restart unless-stopped \
        -p "$OVERPASS_PORT:80" \
        -v "$overpass_dir/db:/db" \
        -v "$overpass_dir:/osm" \
        -e OVERPASS_META=yes \
        -e OVERPASS_MODE=init \
        -e OVERPASS_PLANET_URL=file:///osm/europe-latest.osm.pbf \
        -e OVERPASS_RULES_LOAD=10 \
        wiktorn/overpass-api

    echo "Overpass is importing data. This runs in the background."
    echo "Check progress with: docker logs -f overpass-europe"
    echo "Once import completes, Overpass will be at http://localhost:$OVERPASS_PORT/api/interpreter"
}

start_overpass() {
    local overpass_dir="$DATA_DIR/overpass"
    docker stop overpass-europe 2>/dev/null && docker rm overpass-europe 2>/dev/null || true
    echo "Starting Overpass on port $OVERPASS_PORT..."
    docker run -d --name overpass-europe --restart unless-stopped \
        -p "$OVERPASS_PORT:80" \
        -v "$overpass_dir/db:/db" \
        -e OVERPASS_META=yes \
        -e OVERPASS_MODE=clone \
        -e OVERPASS_RULES_LOAD=10 \
        wiktorn/overpass-api
    echo "Overpass running at http://localhost:$OVERPASS_PORT/api/interpreter"
}

# ── Main ─────────────────────────────────────────────────────
status() {
    echo "=== Service Status ==="
    for name in osrm-europe overpass-europe; do
        if docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
            local ports
            ports=$(docker port "$name" 2>/dev/null | head -1)
            echo "  $name: RUNNING ($ports)"
        else
            echo "  $name: STOPPED"
        fi
    done
    echo ""
    echo "Disk usage:"
    du -sh "$DATA_DIR"/* 2>/dev/null || echo "  No data yet"
}

case "${1:-help}" in
    setup)
        case "${2:-all}" in
            all)      setup_osrm; echo ""; setup_overpass ;;
            osrm)     setup_osrm ;;
            overpass) setup_overpass ;;
            *)        echo "Usage: $0 setup [osrm|overpass|all]"; exit 1 ;;
        esac
        echo ""
        echo "Add to your shell profile:"
        echo "  export OSRM_URL=http://localhost:$OSRM_PORT"
        echo "  export OVERPASS_URL=http://localhost:$OVERPASS_PORT/api/interpreter"
        ;;
    start)
        start_osrm 2>/dev/null || echo "OSRM: no data (run setup first)"
        start_overpass 2>/dev/null || echo "Overpass: no data (run setup first)"
        ;;
    stop)
        docker stop osrm-europe overpass-europe 2>/dev/null || true
        docker rm osrm-europe overpass-europe 2>/dev/null || true
        echo "All services stopped."
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {setup|start|stop|status}"
        echo ""
        echo "  setup [osrm|overpass|all]  Download, process, and start services"
        echo "  start                      Start existing services"
        echo "  stop                       Stop all services"
        echo "  status                     Show what's running"
        ;;
esac
