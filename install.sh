#!/bin/bash
# wechat-daily installer — symlinks the CLI onto your PATH and runs init.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SRC/wechat_daily.py"
chmod +x "$SCRIPT"

# pick a bin dir on PATH that we can write to
for d in "$HOME/.local/bin" /usr/local/bin; do
  if [ -d "$d" ] && [ -w "$d" ]; then BIN="$d"; break; fi
done
[ -z "$BIN" ] && { mkdir -p "$HOME/.local/bin"; BIN="$HOME/.local/bin"; }

ln -sf "$SCRIPT" "$BIN/wechat-daily"
echo "✓ linked: $BIN/wechat-daily -> $SCRIPT"
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "⚠ add $BIN to your PATH (e.g. echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc)";;
esac

echo
echo "Prerequisite (if not already installed):  npm install -g @canghe_ai/wechat-cli"
echo "Then run:  wechat-daily init"
