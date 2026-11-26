#!/usr/bin/env bash
# Download only the models chosen in the hardware-aware installer prompt.
set -Eeuo pipefail
INSTALL_DIR=${1:?installation directory required}
ENV_FILE="$INSTALL_DIR/.env"
MODEL_CACHE=$(sed -n 's/^AI_MODEL_CACHE=//p' "$ENV_FILE" | head -n 1)
SELECTED=$(sed -n 's/^AI_SELECTED_MODELS=//p' "$ENV_FILE" | head -n 1)
[[ $MODEL_CACHE == /* && -n $SELECTED ]] || { echo 'Missing model cache or selection' >&2; exit 1; }
mkdir -p "$MODEL_CACHE/gguf"

gguf() {
  local repo=$1 file=$2 min_bytes=$3 destination="$MODEL_CACHE/gguf/$2" partial="$MODEL_CACHE/gguf/$2.part"
  if [[ -s $destination && $(stat -c %s "$destination") -ge $min_bytes ]]; then
    printf 'Already downloaded: %s\n' "$destination"
    return
  fi
  printf 'Downloading %s into %s\n' "$repo/$file" "$partial"
  curl --fail --location --retry 5 --retry-delay 3 --continue-at - \
    --output "$partial" "https://huggingface.co/$repo/resolve/main/$file"
  [[ $(stat -c %s "$partial") -ge $min_bytes ]] || { echo "Download incomplete: $partial" >&2; exit 1; }
  mv "$partial" "$destination"
}

hf_repo() {
  local repo=$1
  printf 'Downloading selected Hugging Face model: %s\n' "$repo"
  sudo docker run --rm -e HF_HOME=/root/.cache/huggingface \
    -v "$MODEL_CACHE:/root/.cache/huggingface" --entrypoint python \
    vllm/vllm-openai:v0.32.0-ubuntu2404 \
    -c 'import sys; from huggingface_hub import snapshot_download; snapshot_download(sys.argv[1])' "$repo"
}

IFS=, read -ra models <<< "$SELECTED"
for model in "${models[@]}"; do
  case "$model" in
    cpu-tiny) gguf Qwen/Qwen3-0.6B-GGUF Qwen3-0.6B-Q8_0.gguf 600000000 ;;
    cpu-4b) gguf Qwen/Qwen3-4B-GGUF Qwen3-4B-Q4_K_M.gguf 2400000000 ;;
    qwen4b) hf_repo Qwen/Qwen3-4B-Instruct-2507 ;;
    llm) hf_repo cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit ;;
    huihui) gguf huihui-ai/Huihui-Qwen3.6-35B-A3B-abliterated-MTP-GGUF Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q3_K.gguf 16000000000
            ln -sf Huihui-Qwen3.6-35B-A3B-abliterated-ggml-model-Q3_K.gguf "$MODEL_CACHE/gguf/huihui-qwen3.6-35b-a3b-q3_k.gguf" ;;
    flint-iq4) gguf bartowski/RecursiveSI_flint-GGUF RecursiveSI_flint-IQ4_XS.gguf 10000000000 ;;
    flint-q3) gguf bartowski/RecursiveSI_flint-GGUF RecursiveSI_flint-Q3_K_M.gguf 10000000000 ;;
    deepseek) gguf mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q5_K_M.gguf 3000000000 ;;
    qwen38) gguf IsValorum/Qwen3.8-35B-A3B-Distill-MTP-APEX-I-MiniPlus-V2.1-Abliterated-GGUF Qwen3.8-35B-A3B-Distill.APEX-I-MiniPlus-V2.1-Abliterated.gguf 10000000000 ;;
    gemma4) gguf bullerwins/Huihui-gemma-4-26B-A4B-it-abliterated-GGUF Huihui-gemma-4-26B-A4B-it-abliterated-Q4_K_M.gguf 10000000000 ;;
    vision) hf_repo QuantTrio/Qwen3-VL-30B-A3B-Instruct-AWQ ;;
    media) ;; # ComfyUI runtime; image/video models are chosen in the switcher later.
    *) echo "Unsupported selected model: $model" >&2; exit 2 ;;
  esac
done
