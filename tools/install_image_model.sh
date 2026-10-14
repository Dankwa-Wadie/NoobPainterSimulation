#!/usr/bin/env bash
# Install the optional local image model, so the painter can draw a picture from an
# idea (--idea, or "Describe an idea" in the app): ComfyUI with the GGUF loader and
# Qwen-Image 2512 (Q4), its text encoder, VAE and an 8-step Lightning add-on.
#
#   ~20.4 GB of downloads; needs ~25 GB free. Resumable: run it again to continue.
#   Installs into $NOOBPAINTER_COMFY_HOME (default ~/ComfyUI-Installs/ComfyUI).
#   Runs on Apple Silicon (Metal) or an NVIDIA GPU; ~16 GB of memory or more.
set -euo pipefail

HOME_DIR="${NOOBPAINTER_COMFY_HOME:-$HOME/ComfyUI-Installs/ComfyUI}"
COMFY="$HOME_DIR/ComfyUI"
PY="$HOME_DIR/standalone-env/bin/python3"
M="$COMFY/models"
step() { echo "STEP $1/6: $2"; }

need_gb=25
free_gb=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1024/1024)}')
if [ "$free_gb" -lt "$need_gb" ]; then
  echo "ERROR: only ${free_gb} GB free; the image model needs about ${need_gb} GB." >&2
  exit 1
fi

step 1 "ComfyUI"
mkdir -p "$HOME_DIR"
if [ ! -d "$COMFY/.git" ]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY"
fi

step 2 "its Python environment"
if [ ! -x "$PY" ]; then
  python3 -m venv "$HOME_DIR/standalone-env"
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$COMFY/requirements.txt"

step 3 "the GGUF loader (city96/ComfyUI-GGUF)"
if [ ! -d "$COMFY/custom_nodes/ComfyUI-GGUF" ]; then
  git clone --depth 1 https://github.com/city96/ComfyUI-GGUF "$COMFY/custom_nodes/ComfyUI-GGUF"
fi
"$PY" -m pip install -q -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"

fetch() {  # url, destination: resumable, quiet but shows progress
  mkdir -p "$(dirname "$2")"
  echo "  $(basename "$2")"
  curl -fL -C - --retry 20 --retry-delay 5 --progress-bar -o "$2" "$1"
}
HF=https://huggingface.co

step 4 "Qwen-Image 2512 (13.2 GB)"
fetch "$HF/unsloth/Qwen-Image-2512-GGUF/resolve/main/qwen-image-2512-Q4_K_M.gguf" \
      "$M/unet/qwen-image-2512-Q4_K_M.gguf"

step 5 "the text encoder (6.0 GB)"
fetch "$HF/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf" \
      "$M/text_encoders/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
fetch "$HF/unsloth/Qwen2.5-VL-7B-Instruct-GGUF/resolve/main/mmproj-F16.gguf" \
      "$M/text_encoders/Qwen2.5-VL-7B-Instruct-mmproj-F16.gguf"

step 6 "the VAE and the 8-step add-on (1.1 GB)"
fetch "$HF/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors" \
      "$M/vae/qwen_image_vae.safetensors"
fetch "$HF/lightx2v/Qwen-Image-2512-Lightning/resolve/main/Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors" \
      "$M/loras/Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors"

echo "DONE: the local image model is installed in $HOME_DIR"
