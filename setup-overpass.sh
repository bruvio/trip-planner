#!/bin/bash
# Setup local Overpass API with country-specific extracts
# Merges GB + France + Italy + Germany + Switzerland into one database
#
# Usage:
#   ./setup-overpass.sh          # Merge extracts + import + start
#   ./setup-overpass.sh start    # Start from existing database
#   ./setup-overpass.sh stop     # Stop container
#   ./setup-overpass.sh status   # Check status
#
# After setup:
#   export OVERPASS_URL=http://localhost:12346/api/interpreter

set -euo pipefail

DATA_DIR="${TRIP_PLANNER_DATA:-$HOME/.trip-planner-data}"
EXTRACTS_DIR="$DATA_DIR/overpass-extracts"
OVERPASS_DIR="$DATA_DIR/overpass"
MERGED_PBF="$OVERPASS_DIR/merged-europe.osm.pbf"
PORT="${OVERPASS_PORT:-12346}"
CONTAINER="overpass-europe"

check_downloads() {
    local missing=0
    for f in great-britain france italy germany switzerland; do
        local file="$EXTRACTS_DIR/${f}-latest.osm.pbf"
        if [ ! -f "$file" ]; then
            echo "Missing: $file"
            missing=1
        else
            local size
            size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)
            if [ "$size" -lt 100000000 ]; then
                echo "Incomplete: $file ($(numfmt --to=iec $size) -- still downloading?)"
                missing=1
            fi
        fi
    done
    return $missing
}

merge_extracts() {
    echo "Merging country extracts with osmium (via Docker)..."
    mkdir -p "$OVERPASS_DIR"

    docker run --rm \
        -v "$EXTRACTS_DIR:/extracts:ro" \
        -v "$OVERPASS_DIR:/output" \
        stefda/osmium-tool \
        osmium merge \
            /extracts/great-britain-latest.osm.pbf \
            /extracts/france-latest.osm.pbf \
            /extracts/italy-latest.osm.pbf \
            /extracts/germany-latest.osm.pbf \
            /extracts/switzerland-latest.osm.pbf \
            -o /output/merged-europe.osm.pbf --overwrite

    echo "Merged to $MERGED_PBF ($(du -h "$MERGED_PBF" | cut -f1))"
}

import_overpass() {
    echo "Importing into Overpass (this takes 15-30 min)..."
    docker stop "$CONTAINER" 2>/dev/null; docker rm "$CONTAINER" 2>/dev/null || true
    mkdir -p "$OVERPASS_DIR/db"

    docker run -d --name "$CONTAINER" --restart unless-stopped \
        -p "$PORT:80" \
        -v "$OVERPASS_DIR/db:/db" \
        -v "$OVERPASS_DIR:/osm:ro" \
        -e OVERPASS_META=yes \
        -e OVERPASS_MODE=init \
        -e OVERPASS_PLANET_URL=file:///osm/merged-europe.osm.pbf \
        -e OVERPASS_RULES_LOAD=10 \
        wiktorn/overpass-api

    echo "Import started. Monitor with: docker logs -f $CONTAINER"
    echo "When you see 'listening' in the logs, Overpass is ready."
    echo ""
    echo "Test with:"
    echo "  curl -s 'http://localhost:$PORT/api/interpreter' -d 'data=[out:json];node(51.75,-1.26,51.76,-1.25);out 1;'"
}

start_overpass() {
    docker stop "$CONTAINER" 2>/dev/null; docker rm "$CONTAINER" 2>/dev/null || true
    echo "Starting Overpass on port $PORT..."
    docker run -d --name "$CONTAINER" --restart unless-stopped \
        -p "$PORT:80" \
        -v "$OVERPASS_DIR/db:/db" \
        -e OVERPASS_META=yes \
        -e OVERPASS_MODE=clone \
        -e OVERPASS_RULES_LOAD=10 \
        wiktorn/overpass-api
    echo "Overpass running at http://localhost:$PORT/api/interpreter"
}

case "${1:-setup}" in
    setup)
        echo "=== Checking downloads ==="
        if ! check_downloads; then
            echo ""
            echo "Downloads not complete. Wait for them to finish, then re-run."
            echo "Monitor: watch -n 5 'ls -lh $EXTRACTS_DIR/'"
            exit 1
        fi
        echo "All extracts present."
        echo ""
        merge_extracts
        echo ""
        import_overpass
        ;;
    start)
        if [ ! -d "$OVERPASS_DIR/db" ]; then
            echo "No database. Run ./setup-overpass.sh first."
            exit 1
        fi
        start_overpass
        ;;
    stop)
        docker stop "$CONTAINER" 2>/dev/null; docker rm "$CONTAINER" 2>/dev/null || true
        echo "Stopped."
        ;;
    status)
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
            echo "Overpass: RUNNING (port $PORT)"
            docker logs "$CONTAINER" --tail 3
        else
            echo "Overpass: STOPPED"
        fi
        echo ""
        echo "Database: $(du -sh "$OVERPASS_DIR/db" 2>/dev/null | cut -f1 || echo 'not created')"
        echo "Extracts:"
        ls -lh "$EXTRACTS_DIR/" 2>/dev/null || echo "  none"
        ;;
    *)
        echo "Usage: $0 {setup|start|stop|status}"
        ;;
esac
