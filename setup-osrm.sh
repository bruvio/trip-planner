#!/bin/bash
# Setup self-hosted OSRM with Europe map data
# This gives you fast routing (<100ms) with full exclude support (toll, ferry, motorway)
#
# Requirements: docker, ~30GB disk space, ~30min processing time
#
# Usage:
#   ./setup-osrm.sh              # Download + process + start
#   ./setup-osrm.sh start        # Start existing processed data
#   ./setup-osrm.sh stop         # Stop the container
#
# After setup, use with trip_planner.py:
#   export OSRM_URL=http://localhost:5000
#   python trip_planner.py --from "Oxford" --to "Rome" --route-mode compare

set -euo pipefail

OSRM_DIR="${OSRM_DATA_DIR:-$HOME/.osrm-data}"
CONTAINER_NAME="osrm-europe"
PORT="${OSRM_PORT:-5000}"
PBF_URL="https://download.geofabrik.de/europe-latest.osm.pbf"
PBF_FILE="$OSRM_DIR/europe-latest.osm.pbf"

start_server() {
    echo "Starting OSRM server on port $PORT..."
    docker run -d \
        --name "$CONTAINER_NAME" \
        --restart unless-stopped \
        -p "$PORT:5000" \
        -v "$OSRM_DIR:/data" \
        osrm/osrm-backend \
        osrm-routed --algorithm mld /data/europe-latest.osrm
    echo "OSRM running at http://localhost:$PORT"
    echo ""
    echo "Add to your shell profile:"
    echo "  export OSRM_URL=http://localhost:$PORT"
}

stop_server() {
    echo "Stopping OSRM..."
    docker stop "$CONTAINER_NAME" 2>/dev/null && docker rm "$CONTAINER_NAME" 2>/dev/null
    echo "Stopped."
}

case "${1:-setup}" in
    start)
        if [ ! -f "$OSRM_DIR/europe-latest.osrm" ]; then
            echo "ERROR: No processed data found at $OSRM_DIR"
            echo "Run './setup-osrm.sh' first to download and process the map."
            exit 1
        fi
        stop_server 2>/dev/null || true
        start_server
        ;;

    stop)
        stop_server
        ;;

    setup)
        mkdir -p "$OSRM_DIR"

        # Download Europe extract
        if [ ! -f "$PBF_FILE" ]; then
            echo "Downloading Europe map (~25GB)..."
            echo "This will take a while depending on your connection."
            wget -c -O "$PBF_FILE" "$PBF_URL"
        else
            echo "PBF file already exists, skipping download."
        fi

        # Extract
        echo ""
        echo "Extracting road network (this takes ~15 min)..."
        docker run --rm -v "$OSRM_DIR:/data" \
            osrm/osrm-backend \
            osrm-extract -p /opt/car.lua /data/europe-latest.osm.pbf

        # Partition
        echo ""
        echo "Partitioning (this takes ~5 min)..."
        docker run --rm -v "$OSRM_DIR:/data" \
            osrm/osrm-backend \
            osrm-partition /data/europe-latest.osrm

        # Customize
        echo ""
        echo "Customizing (this takes ~5 min)..."
        docker run --rm -v "$OSRM_DIR:/data" \
            osrm/osrm-backend \
            osrm-customize /data/europe-latest.osrm

        echo ""
        echo "Processing complete. Starting server..."
        stop_server 2>/dev/null || true
        start_server

        echo ""
        echo "Done! Test with:"
        echo "  curl 'http://localhost:$PORT/route/v1/driving/-1.26,51.75;4.83,45.76?overview=false'"
        echo ""
        echo "Use with trip_planner.py:"
        echo "  export OSRM_URL=http://localhost:$PORT"
        echo "  python trip_planner.py --from Oxford --to Rome --route-mode compare"
        ;;

    *)
        echo "Usage: $0 [setup|start|stop]"
        exit 1
        ;;
esac
