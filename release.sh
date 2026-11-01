#!/bin/bash
# Cut a release: ./release.sh 9 "what changed"
# Bumps VERSION and pushes main; GitHub Actions tests, builds course-kit.zip and publishes release v9.
set -e
cd "$(dirname "$0")"
v="$1"; notes="${2:-Course Kit v$1}"
[ -n "$v" ] || { echo "usage: ./release.sh <version> [notes]"; exit 1; }
echo "$v" > VERSION
git add -A
git commit -m "Release v$v

$notes"
git push -u origin HEAD
echo "Pushed. Release v$v will appear at https://github.com/Kaden-G/course-kit/releases in a few minutes."
