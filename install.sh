#!/usr/bin/env bash
# NoobPainterSimulation: install everything with one line in a terminal.
#
#   curl -fsSL https://raw.githubusercontent.com/Dankwa-Wadie/NoobPainterSimulation/main/install.sh | bash
#
# It installs what's missing (git, Python 3, ffmpeg, Rust), downloads this project
# into ~/NoobPainterSimulation, sets up its Python environment, builds the paint
# engine, fetches the music, and offers the optional local image model (~20 GB).
#
# Options (set before `bash`):  NOOBPAINTER_DIR=~/somewhere   WITH_IMAGE_MODEL=yes|no
set -euo pipefail

DIR="${NOOBPAINTER_DIR:-$HOME/NoobPainterSimulation}"
REPO="https://github.com/Dankwa-Wadie/NoobPainterSimulation"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
OS="$(uname -s)"

say "Checking the tools it needs"
if [ "$OS" = "Darwin" ]; then
  if ! have brew; then
    say "Installing Homebrew (the macOS package manager; it may ask for your password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
  fi
  for pkg in git python@3.12 ffmpeg; do
    brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
  done
  PYTHON="$(brew --prefix)/bin/python3.12"
elif [ "$OS" = "Linux" ]; then
  if have apt-get; then
    sudo apt-get update -y && sudo apt-get install -y git python3 python3-venv python3-pip ffmpeg build-essential curl
  elif have dnf; then
    sudo dnf install -y git python3 python3-pip ffmpeg gcc gcc-c++ make curl
  else
    echo "Please install git, Python 3.10+, ffmpeg and a C compiler, then run this again." >&2; exit 1
  fi
  PYTHON=python3
else
  echo "This installer supports macOS and Linux. On Windows, use WSL (Ubuntu) and run it there." >&2; exit 1
fi

if ! have cargo && [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  say "Installing Rust (to build the paint engine)"
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
export PATH="$HOME/.cargo/bin:$PATH"

say "Downloading NoobPainterSimulation into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

say "Setting up Python (a private environment in $DIR/.venv)"
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

say "Building the paint engine (a few minutes, the first time)"
./setup.sh

say "Fetching the music (free public-domain recordings)"
.venv/bin/python tools/fetch_classical.py || echo "  (music skipped; videos will be silent until you run tools/fetch_classical.py)"

choice="${WITH_IMAGE_MODEL:-ask}"
if [ "$choice" = "ask" ] && [ -r /dev/tty ]; then
  printf '\nInstall the local image model too? It lets you paint from a typed idea\n'
  printf 'instead of a picture. About 20 GB to download. You can also do it later from the app. [y/N] '
  read -r answer < /dev/tty || answer=n
  case "$answer" in [yY]*) choice=yes ;; *) choice=no ;; esac
fi
if [ "$choice" = "yes" ]; then
  say "Installing the local image model (about 20 GB)"
  bash tools/install_image_model.sh
fi

if [ "$OS" = "Darwin" ] && [ ! -d /Applications/Blender.app ]; then
  echo
  echo "Optional: the carving styles need Blender (free): https://www.blender.org/download/"
fi

say "All set. To open the app:"
echo "    cd \"$DIR\" && .venv/bin/python -m noobpainter ui"
echo
if [ -r /dev/tty ]; then
  printf 'Open it now? [Y/n] '
  read -r go < /dev/tty || go=n
  case "$go" in [nN]*) ;; *) exec .venv/bin/python -m noobpainter ui ;; esac
fi
