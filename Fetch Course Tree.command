#!/bin/bash
# Double-click me. First run installs uv (a tiny Python manager) and the browser automation library.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Applications/Google Chrome.app" ] && [ ! -d "/Applications/Microsoft Edge.app" ]; then
  echo "No Google Chrome found; installing Playwright's own Chromium (one time, ~150 MB)..."
  uv run --with playwright==1.63.0 python -m playwright install chromium
fi
echo
echo "A browser window will open. Log in there (CAC / username). This window will list the course when done."
echo
uv run fetch_moodle.py --tree
echo
echo "Done. You can close this window."
