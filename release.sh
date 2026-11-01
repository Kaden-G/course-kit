#!/bin/bash
# Cut a release: ./release.sh 9
# Pushes tag v9; GitHub Actions runs the tests, builds course-kit.zip and publishes the release.
set -e
cd "$(dirname "$0")"
v="$1"
[ -n "$v" ] || { echo "usage: ./release.sh <version>"; exit 1; }
echo "$v" > VERSION
git add -A
git commit -m "Release v$v" || true
git push -u origin HEAD
git tag -a "v$v" -m "Course Kit v$v"
git push origin "v$v"
echo "Tag v$v pushed. Watch: https://github.com/Kaden-G/course-kit/actions"
