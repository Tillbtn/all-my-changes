#!/usr/bin/env bash
# Publish web/ (including the generated per-user data) to GitHub Pages as a
# single-commit gh-pages branch - history stays small no matter how often
# this runs. One-time GitHub setup: Settings -> Pages -> "Deploy from a
# branch" -> gh-pages / root.
#
# GitHub refuses files over 100 MB and asks Pages sites to stay under 1 GB,
# so the payload is checked before anything is pushed. build_output.py keeps
# it small by gzipping the GeoJSON; if a file still trips the limit, rebuild
# that user with a coarser AMC_SIMPLIFY / AMC_PRECISION or a lower
# AMC_VERTEX_CAP (see the head of pipeline/build_output.py).
set -euo pipefail
cd "$(dirname "$0")"

FILE_LIMIT=$((100 * 1024 * 1024))
SITE_LIMIT=$((1024 * 1024 * 1024))

fail=0
while IFS= read -r -d '' f; do
  size=$(stat -c%s "$f")
  if [ "$size" -gt "$FILE_LIMIT" ]; then
    echo "too big for git: ${f#./} ($((size / 1024 / 1024)) MB > 100 MB)" >&2
    fail=1
  fi
done < <(find web -type f -print0)

total=$(du -sb web | cut -f1)
echo "payload: $((total / 1024 / 1024)) MB in $(find web -type f | wc -l) files"
if [ "$total" -gt "$SITE_LIMIT" ]; then
  echo "over the 1 GB GitHub Pages limit" >&2
  fail=1
fi
[ "$fail" = 0 ] || { echo "aborting deploy." >&2; exit 1; }

remote=$(git remote get-url origin)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cp -r web/. "$tmp"/
touch "$tmp/.nojekyll"
git -C "$tmp" init -q -b gh-pages
git -C "$tmp" add -A
git -C "$tmp" commit -qm "deploy $(date -u +%Y-%m-%dT%H:%MZ)"
git -C "$tmp" push -f "$remote" gh-pages

echo "Pushed to $remote (branch gh-pages)."
