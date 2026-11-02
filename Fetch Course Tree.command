#!/bin/bash
# Double-click me. First run installs uv (a tiny Python manager) and the browser automation library.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo
echo "A browser window will open. Log in there (CAC / username). This window will list the course when done."
echo "(No Google Chrome? The first run downloads a private copy of Chromium, about 150 MB.)"
echo
uv run fetch_moodle.py --tree
echo
echo "Done. You can close this window."
