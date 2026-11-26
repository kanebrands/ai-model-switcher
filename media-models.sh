#!/usr/bin/env bash
# Optional, resumable downloads for the ComfyUI image and video templates.
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $DIR/.env ]] || { echo 'Run ai_setup.sh first.' >&2; exit 1; }
MEDIA_DIR=$(sed -n 's/^AI_MEDIA_DIR=//p' "$DIR/.env" | head -n 1)
[[ $MEDIA_DIR == /* ]] || { echo 'AI_MEDIA_DIR must be absolute.' >&2; exit 1; }
ROOT="$MEDIA_DIR/models"

download() {
  local subdir=$1 filename=$2 url=$3 dest="$ROOT/$1/$2" partial="$ROOT/$1/$2.part"
  mkdir -p "$ROOT/$subdir"
  if [[ -s $dest ]]; then
    printf 'Already present: %s\n' "$dest"
    return
  fi
  printf '\nDownloading %s\nPartial file: %s\n' "$filename" "$partial"
  curl --fail --location --retry 5 --retry-delay 3 --continue-at - --output "$partial" "$url"
  [[ -s $partial ]] || { echo "Empty download: $partial" >&2; exit 1; }
  mv "$partial" "$dest"
}

image() {
  download checkpoints flux1-schnell-fp8.safetensors \
    'https://huggingface.co/Comfy-Org/Flux-1-Schnell/resolve/main/flux1-schnell-fp8.safetensors'
}
video() {
  local base='https://huggingface.co/Comfy-Org/Wan-2.2/resolve/main/split_files'
  download diffusion_models wan2.2_ti2v_5B_fp16.safetensors "$base/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors"
  download text_encoders umt5_xxl_fp8_e4m3fn_scaled.safetensors "$base/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  download vae wan2.2_vae.safetensors "$base/vae/wan2.2_vae.safetensors"
}

case "${1:-help}" in
  image) image ;;
  video) video ;;
  all) image; video ;;
  *) echo 'Usage: media-models.sh {image|video|all}' >&2; exit 2 ;;
esac
