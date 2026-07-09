#!/usr/bin/env bash
# Serve the map locally at http://localhost:8080
cd "$(dirname "$0")/web"
exec python3 -m http.server "${1:-8080}"
