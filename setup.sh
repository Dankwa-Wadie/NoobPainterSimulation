#!/usr/bin/env bash
# Fetch and build the paint simulator (claude-paint, MIT) with the one change this
# project needs: a hand_secs() clock, so the playback can put the hand on each stroke.
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
# every pigment box (the modern palette needs cadmiums, ultramarine, viridian...)
(cd vendor/claude-paint && cargo build --release -p easel --features all-boxes)
echo "built: vendor/claude-paint/target/release/easel"
