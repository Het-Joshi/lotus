#!/bin/sh
# Install lotus, a local agent harness for Ollama.
#
#   curl -fsSL https://raw.githubusercontent.com/Het-Joshi/lotus/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/Het-Joshi/lotus/main/install.sh | sh -s -- --browser
#
# Uses uv or pipx when you have them, otherwise a private virtualenv in
# ~/.local/share/lotus with the command linked into ~/.local/bin.
#
# Options (or the matching environment variables):
#   --browser        also install Playwright and Chromium for browser control   (LOTUS_BROWSER=1)
#   --ref <ref>      branch or tag to install, default main                     (LOTUS_REF)
#   --method <m>     uv, pipx or venv instead of picking automatically          (LOTUS_METHOD)
#   --uninstall      remove lotus (your ~/.lotus config and memory are kept)
set -eu

REPO="${LOTUS_REPO:-https://github.com/Het-Joshi/lotus}"
REF="${LOTUS_REF:-main}"
BROWSER="${LOTUS_BROWSER:-0}"
METHOD="${LOTUS_METHOD:-}"
UNINSTALL=0
VENV_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lotus/venv"
BIN_DIR="$HOME/.local/bin"

while [ $# -gt 0 ]; do
  case "$1" in
    --browser) BROWSER=1 ;;
    --ref) REF="$2"; shift ;;
    --method) METHOD="$2"; shift ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) echo "usage: install.sh [--browser] [--ref <branch|tag>] [--method uv|pipx|venv] [--uninstall]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then
  P="$(printf '\033[38;5;169m')"; D="$(printf '\033[2m')"; R="$(printf '\033[0m')"; E="$(printf '\033[31m')"
else
  P=""; D=""; R=""; E=""
fi
say()  { printf '%s❀%s %s\n' "$P" "$R" "$*"; }
note() { printf '  %s%s%s\n' "$D" "$*" "$R"; }
die()  { printf '%s✗ %s%s\n' "$E" "$*" "$R" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  has uv && uv tool uninstall lotus-agent >/dev/null 2>&1 && say "removed the uv tool"
  has pipx && pipx uninstall lotus-agent >/dev/null 2>&1 && say "removed the pipx package"
  if [ -d "$VENV_DIR" ]; then rm -rf "$VENV_DIR"; say "removed $VENV_DIR"; fi
  if [ -L "$BIN_DIR/lotus" ]; then rm -f "$BIN_DIR/lotus"; fi
  say "lotus is uninstalled. Your config and memory are still in ~/.lotus"
  exit 0
fi

# ── what to install ──────────────────────────────────────────────────────────
case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die "this script is for Linux and macOS. On Windows: pip install \"lotus-agent @ $REPO/archive/$REF.tar.gz\"" ;;
esac

PY=""
for cand in python3 python; do
  if has "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done

SRC="${LOTUS_SRC:-$REPO/archive/$REF.tar.gz}"
if [ "$BROWSER" = 1 ]; then SPEC="lotus-agent[browser] @ $SRC"; else SPEC="lotus-agent @ $SRC"; fi

if [ -z "$METHOD" ]; then
  if has uv; then METHOD=uv
  elif has pipx; then METHOD=pipx
  elif [ -n "$PY" ]; then METHOD=venv
  else die "lotus needs Python 3.9 or newer (or uv: https://docs.astral.sh/uv/). Install one and run this again."
  fi
fi

say "installing lotus from $REF with $METHOD"

# ── install ──────────────────────────────────────────────────────────────────
ENV_PY=""
case "$METHOD" in
  uv)
    uv tool install --force --quiet "$SPEC"
    ENV_PY="$(uv tool dir)/lotus-agent/bin/python"
    BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$BIN_DIR")"
    ;;
  pipx)
    pipx install --force "$SPEC" >/dev/null
    ENV_PY="$(pipx environment --value PIPX_LOCAL_VENVS)/lotus-agent/bin/python"
    BIN_DIR="$(pipx environment --value PIPX_BIN_DIR)"
    ;;
  venv)
    [ -n "$PY" ] || die "no Python 3.9+ found"
    "$PY" -m venv --help >/dev/null 2>&1 || die "Python's venv module is missing (Debian/Ubuntu: sudo apt install python3-venv)"
    rm -rf "$VENV_DIR"
    "$PY" -m venv "$VENV_DIR" || die "couldn't create $VENV_DIR (Debian/Ubuntu: sudo apt install python3-venv)"
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV_DIR/bin/python" -m pip install --quiet "$SPEC"
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV_DIR/bin/lotus" "$BIN_DIR/lotus"
    ENV_PY="$VENV_DIR/bin/python"
    ;;
  *) die "unknown method '$METHOD' (use uv, pipx or venv)" ;;
esac

if [ "$BROWSER" = 1 ]; then
  say "installing Chromium for browser control"
  "$ENV_PY" -m playwright install chromium >/dev/null || note "Chromium didn't install; run: $ENV_PY -m playwright install chromium"
  say "installing the stealth browser (a patched Firefox, ~250 MB)"
  "$ENV_PY" -m invisible_playwright fetch >/dev/null 2>&1 || note "the stealth browser didn't install (it needs Python 3.11+); lotus uses plain Chromium until you run: $ENV_PY -m invisible_playwright fetch"
fi

# ── after ────────────────────────────────────────────────────────────────────
VERSION="$("$BIN_DIR/lotus" --version 2>/dev/null || echo lotus)"
say "installed $VERSION → $BIN_DIR/lotus"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) note "$BIN_DIR isn't on your PATH yet. Add this to your shell profile:"
     note "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

if ! has ollama; then
  note "lotus runs models through Ollama, which isn't installed: https://ollama.com/download"
  note "then pull a small model, e.g.  ollama pull qwen3:4b"
fi

note "start it with:  lotus        check your setup with:  lotus doctor"
