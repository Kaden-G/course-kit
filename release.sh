#!/bin/bash
# Cut a release: ./release.sh 9 "what changed"
set -e
cd "$(dirname "$0")"
v="$1"; notes="${2:-}"
[ -n "$v" ] || { echo "usage: ./release.sh <version> [notes]"; exit 1; }
echo "$v" > VERSION
python3 -m pytest -q tests
rm -rf dist && mkdir -p dist
zip -qr dist/course-kit.zip . -x 'dist/*' '.browser-profile/*' 'course/*' 'drafts/*' '*/__pycache__/*' '.git/*' '.pytest_cache/*' 'server.log'
git add -A
git commit -m "Release v$v" || true
git push -u origin HEAD
gh release create "v$v" dist/course-kit.zip --title "Course Kit v$v" --notes "$notes"
echo "Released v$v"
