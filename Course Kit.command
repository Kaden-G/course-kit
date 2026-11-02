#!/bin/bash
# Double-click me. Opens the Course Kit dashboard in your browser. Leave this window open while you use it.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv did not install. Check your internet connection, then open this file again."
  echo "If it keeps failing, send Kaden a screenshot of this window."
  read -r -p "Press Enter to close."
  exit 1
fi
echo "Starting Course Kit at http://127.0.0.1:8765  (close this window to stop it)"
while true; do
  uv run app.py
  code=$?
  [ "$code" -eq 75 ] || break
  echo; echo "Update installed. Restarting..."; echo
done
if [ "$code" -ne 0 ]; then
  echo; echo "Course Kit stopped with an error (code $code). Send Kaden a screenshot of this window."
  read -r -p "Press Enter to close."
fi
