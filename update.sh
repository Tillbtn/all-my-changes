#!/usr/bin/env bash
# Run the pipeline for every user in users.txt (or for the usernames given
# as arguments): fetch new changesets, resolve geometries, rebuild the web
# data. Safe to run daily (e.g. via cron or a systemd timer) - all fetched
# changesets are cached, so incremental runs only cost a couple of API
# requests per user.
#
# --quick builds instant previews from QLever instead (last-editor
# semantics, no edit history/deletions); completed full datasets are never
# touched, and a later full run replaces the preview cleanly.
#
# Every step timestamps its own log lines and reports its runtime; this
# script adds the per-step, per-user and total wall times on top. Set
# AMC_VERBOSE=2 to also log every single API/QLever/Overpass request, or
# AMC_VERBOSE=0 for just the summaries. `.venv/bin/python pipeline/status.py`
# shows where each user stands, including how old their web output is.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python

quick=0
if [ "${1:-}" = "--quick" ]; then
  quick=1
  shift
fi

if [ $# -gt 0 ]; then
  users=("$@")
else
  mapfile -t users < <(grep -vE '^[[:space:]]*(#|$)' users.txt)
fi

# hh:mm:ss for a number of seconds
dur() { printf '%dh%02dm%02ds' $(($1 / 3600)) $((($1 % 3600) / 60)) $(($1 % 60)); }

run_step() {
  local step=$1 t0=$SECONDS
  $PY -u "pipeline/${step}.py"
  echo "----- ${step}: $(dur $((SECONDS - t0)))"
}

run_start=$SECONDS
n=${#users[@]}
i=0
for user in "${users[@]}"; do
  i=$((i + 1))
  user_start=$SECONDS
  echo "===== ${user} (${i}/${n}) ====="
  export OSM_USER="$user"
  if [ "$quick" = 1 ]; then
    run_step quick_qlever
  else
    for step in fetch_changesets parse_changesets resolve_pbf resolve_qlever \
                resolve_api resolve_overpass build_output; do
      run_step "$step"
    done
  fi
  echo "===== ${user} done in $(dur $((SECONDS - user_start)))"
done

echo "All ${n} user(s) in $(dur $((SECONDS - run_start)))."
echo "Serve the map with: ./serve.sh - publish with: ./deploy.sh"
