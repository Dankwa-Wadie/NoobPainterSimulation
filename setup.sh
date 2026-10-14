#!/usr/bin/env bash
# Fetch and build the paint simulator (claude-paint, MIT) with the changes this
# project needs: a hand_secs() clock, so the playback can put the hand on each stroke,
# and a build that compiles on Windows.
# Needs: git, Rust (https://rustup.rs), a C compiler. Python deps: requirements.txt.
set -euo pipefail
cd "$(dirname "$0")"
COMMIT=059b3a211cd5a916ac27128cec640f1905b02d80
if [ ! -d vendor/claude-paint ]; then
  mkdir -p vendor
  # only the code: the full repo carries its gallery of paintings (hundreds of MB)
  git clone --filter=blob:none --no-checkout https://github.com/aliceisjustplaying/claude-paint vendor/claude-paint
  git -C vendor/claude-paint sparse-checkout init --no-cone
  printf '/*\n!/*/\n/crates/easel/\n/crates/paint/\n/.cargo/\n' > vendor/claude-paint/.git/info/sparse-checkout
  git -C vendor/claude-paint checkout -q "$COMMIT"
fi
if ! grep -q hand_secs vendor/claude-paint/crates/easel/src/api.rs; then
  git -C vendor/claude-paint apply "$PWD/patches/claude-paint-hand-secs.patch"
fi
# Windows has no Unix sockets: build without the live session server (`easel run` is all
# this project uses). A no-op on macOS and Linux, applied everywhere so the code is one.
if [ ! -f vendor/claude-paint/crates/easel/src/uds.rs ]; then
  git -C vendor/claude-paint apply "$PWD/patches/claude-paint-windows.patch"
fi
# ...and the fixed Lua seed in a form MSVC accepts (Windows only; no change elsewhere)
if ! grep -q windows_msvc vendor/claude-paint/.cargo/config.toml; then
  git -C vendor/claude-paint apply "$PWD/patches/claude-paint-windows-seed.patch"
fi
# every pigment box (the modern palette needs cadmiums, ultramarine, viridian...)
(cd vendor/claude-paint && cargo build --release -p easel --features all-boxes)
echo "built: vendor/claude-paint/target/release/easel"
