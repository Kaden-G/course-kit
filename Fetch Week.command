#!/bin/bash
# Double-click me. It lists the course, then asks which section to fetch, as many times as you like.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv run fetch_moodle.py --interactive
echo
echo "Files are in the 'course' folder next to this script. You can close this window."
