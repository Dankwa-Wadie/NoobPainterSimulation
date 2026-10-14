#!/usr/bin/env bash
# NoobPainterSimulation: install everything with one line in a terminal.
#
#   curl -fsSL https://raw.githubusercontent.com/Dankwa-Wadie/NoobPainterSimulation/main/install.sh | bash
#
# It installs what's missing (git, Python 3, ffmpeg, Rust), downloads this project
# into ~/NoobPainterSimulation, sets up its Python environment, builds the paint
# engine, and offers the optional music (~500 MB) and local image model (~20 GB).
#
# Options (set before `bash`):  NOOBPAINTER_DIR=~/somewhere   WITH_IMAGE_MODEL=yes|no   WITH_MUSIC=yes|no
set -euo pipefail

# Everything is inside main(), run on the last line: bash reads the whole script before
# running it, so a command that reads stdin cannot swallow the rest of a piped script.
main() {

DIR="${NOOBPAINTER_DIR:-$HOME/NoobPainterSimulation}"
REPO="https://github.com/Dankwa-Wadie/NoobPainterSimulation"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
OS="$(uname -s)"

say "Checking the tools it needs"
if [ "$OS" = "Darwin" ]; then
  if ! have brew; then
    say "Installing Homebrew (the macOS package manager; it may ask for your password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/null
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
  fi
  for pkg in git python@3.12 ffmpeg; do
    brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg" < /dev/null
  done
  PYTHON="$(brew --prefix)/bin/python3.12"
elif [ "$OS" = "Linux" ]; then
  if have apt-get; then
    sudo apt-get update -y < /dev/null && sudo apt-get install -y git python3 python3-venv python3-pip ffmpeg build-essential curl < /dev/null
  elif have dnf; then
    sudo dnf install -y git python3 python3-pip ffmpeg gcc gcc-c++ make curl < /dev/null
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

# The music is optional: videos can have none, or your own (chosen in the app)
choice="${WITH_MUSIC:-ask}"
if [ "$choice" = "ask" ] && (: < /dev/tty) 2>/dev/null; then
  printf '\nDownload the free calm classical music for the videos? About 500 MB.\n'
  printf 'Without it, videos can be silent or use your own music. You can also get it later in the app. [y/N] '
  read -r answer < /dev/tty || answer=n
  case "$answer" in [yY]*) choice=yes ;; *) choice=no ;; esac
fi
if [ "$choice" = "yes" ]; then
  say "Fetching the music (free public-domain recordings)"
  .venv/bin/python tools/fetch_classical.py || echo "  (music skipped; you can get it later in the app)"
fi

choice="${WITH_IMAGE_MODEL:-ask}"
if [ "$choice" = "ask" ] && (: < /dev/tty) 2>/dev/null; then
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

say "Adding NoobPainterSimulation to your desktop"
if [ "$OS" = "Darwin" ]; then
  # a .command opens in Terminal when double-clicked in Finder
  launcher="$HOME/Desktop/NoobPainterSimulation.command"
  printf '#!/bin/bash\n# Opens NoobPainterSimulation in your browser. Keep this window open while you use it; close it to quit.\ncd %q && exec .venv/bin/python -m noobpainter ui\n' "$DIR" > "$launcher"
  chmod +x "$launcher"
else
  apps="$HOME/.local/share/applications"
  mkdir -p "$apps"
  entry="$apps/noobpainter.desktop"
  cat > "$entry" <<DESKTOP
[Desktop Entry]
Type=Application
Name=NoobPainterSimulation
Comment=Open NoobPainterSimulation in your browser
Path=$DIR
Exec="$DIR/.venv/bin/python" -m noobpainter ui
Icon=$DIR/assets/brand/app.png
Terminal=true
Categories=Graphics;
DESKTOP
  chmod +x "$entry"
  desktop="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  if [ -d "$desktop" ]; then
    cp "$entry" "$desktop/NoobPainterSimulation.desktop"
    chmod +x "$desktop/NoobPainterSimulation.desktop"
    # GNOME runs a desktop launcher only once it is marked trusted
    gio set "$desktop/NoobPainterSimulation.desktop" metadata::trusted true 2>/dev/null || true
  fi
fi

say "All set"
echo "To open it any time: double-click NoobPainterSimulation on your desktop."
echo "It opens in your browser. Keep the terminal window open while you paint; close it to quit."
echo
if (: < /dev/tty) 2>/dev/null; then
  printf 'Open it now? [Y/n] '
  read -r go < /dev/tty || go=n
  case "$go" in [nN]*) ;; *) exec .venv/bin/python -m noobpainter ui ;; esac
fi
}

main "$@"
