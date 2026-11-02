#!/bin/bash
# Double-click me. It lists the course, then asks which section to fetch, as many times as you like.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo
echo "A browser window will open. Log in there (CAC / username). This window will list the course when done."
echo
uv run fetch_moodle.py --interactive
echo
echo "Files are in the 'course' folder next to this script. You can close this window."
